"""The positive corpus: real merchant prose, and what the decomposer may NOT read out of it.

Why this file exists, and why its examples are not written here
---------------------------------------------------------------
This repository has a documented, repeatedly-measured blind spot: a gate checks that a refusal
fires on the attack and never that it stays silent on legitimate traffic. Three defects of
exactly that shape were found and fixed in one day (D58, D59, and the relaxation bug), and the
prose grader had three more.

The rule that follows from it is that **a change which moves a refusal must be driven against a
positive corpus drawn from the repository's own fixtures, never from examples the author of the
change wrote** — because those are authored by the same mind that chose the bound, and they
agree with it by construction. Every sentence graded in this file is quoted verbatim from a
file in this tree, and each one carries the path it came from:

* ``fixtures/real-catalogs/`` — 3,093 products recorded verbatim from ten real Shopify
  storefronts, of which 2,831 carry prose. This is the FALSE-POSITIVE floor: 44,595 real
  merchant sentences that must not cost anybody a ``contradicted``. Its limitation is stated
  rather than glossed — see :func:`test_the_recorded_corpus_mints_only_what_is_true`;
* ``fixtures/golden/golden_set.json`` — the human-APPROVED S8/T-080 answer key. Thirteen pitches,
  each with its own catalogue snapshot and a hand-approved ``expected_status`` per claim. This
  is the corpus that grades in BOTH directions at once, because the humans who approved it
  marked some claims ``verified`` and others ``contradicted``;
* this repository's own pitch GENERATORS — the reference personas, the store agent's
  ``fallback_pitch``, its reviewed recorded-model replies, the served demo ``Bid.message`` and
  the scraped policy-page fixtures. This is the traffic the decomposer actually meets.

A corpus that forces a refusal to be weakened means the duty is failing, not passing. None of
the three below did; the readings this module takes out of every one of them are unchanged by
the guard rewrite, and what changed is only what it takes out of prose that lies about which
product it is talking about.
"""

from __future__ import annotations

import functools
import gzip
import html
import json
import re
from pathlib import Path
from typing import Any

import pytest
from claim_verification.decomposition import sentences
from claim_verification.pitch import decompose_pitch
from claim_verification.verifier import catalog_keys, verify

REPO = Path(__file__).resolve().parents[3]
RECORDED_CATALOGS = REPO / "fixtures" / "real-catalogs" / "stores"
GOLDEN_SET = REPO / "fixtures" / "golden" / "golden_set.json"

VERIFIER_VERSION = "test-real-prose"

# ======================================================================================
# Measured on this corpus on 2026-09-08. A re-collection changes them and is MEANT to fail
# here — updating a number is the deliberate act, the same convention
# ``fixtures/tests/test_real_catalogs.py`` uses on the same corpus.
# ======================================================================================
STOREFRONTS = 10
DOCUMENTS_WITH_PROSE = 2_831
SENTENCES_IN_THE_CORPUS = 44_595
AVAILABILITY_SENTENCES = 289

#: The ONE reading the whole recorded corpus yields, and it is true: the scale really does
#: carry a ten-year warranty. Quoted with its source so a change to this number is legible.
THE_ONLY_TRUE_READING = (
    "purebulk.com",
    "warranty_months",
    120,
    "The scale is backed up by a limited 10 year warranty by the manufacturer.",
)

_TAG = re.compile(r"<[^>]+>")


def _prose(product: Any) -> str:
    """The merchant's own words, out of the one field that carries them.

    Real Shopify ``/products.json`` payloads fold every narrative field into ``body_html``;
    there is no separate description, shipping or returns field. Entities are unescaped twice
    because the recorded bytes carry them doubly escaped.
    """
    raw = product.get("body_html") or ""
    return re.sub(r"[ \t\xa0]+", " ", _TAG.sub(" ", html.unescape(html.unescape(raw)))).strip()


@functools.lru_cache(maxsize=1)
def _recorded_corpus() -> tuple[tuple[str, dict[str, Any], str], ...]:
    """``(storefront, product, prose)`` for every recorded product that has prose.

    Reads bytes off disk and nothing else — this suite runs offline (D3/C9) and the root
    conftest arms ``pytest-socket``. Deliberately not routed through ``ingest``: the trust image
    ships ``packages/verification`` and no ``ingest`` at all, and
    ``test_pitch.py::test_the_verification_package_imports_with_no_ingest_service_present``
    exists because that import once broke two shipped images.
    """
    rows: list[tuple[str, dict[str, Any], str]] = []
    for path in sorted(RECORDED_CATALOGS.glob("*.products.jsonl.gz")):
        storefront = path.name.split(".products")[0]
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                product = json.loads(line)
                prose = _prose(product)
                if prose:
                    rows.append((storefront, product, prose))
    return tuple(rows)


def _snapshot_of(storefront: str, product: dict[str, Any]) -> dict[str, Any]:
    """The platform's own crawl of THIS product, in the shape the exchange holds.

    The prose and the record come out of one recorded document, so the store and the platform
    cannot actually disagree: every ``contradicted`` this snapshot produces is a false
    accusation by construction, which is what makes the corpus a floor rather than a sample.

    ``attributes`` is empty and the stock fact lives on the offer block, because that is what
    ``ingest.adapters.mapping.build_upserts`` emits — no ``attribute`` op at all (D59).
    """
    variants = product.get("variants") or []
    return {
        "snapshot_id": f"real-catalog:{storefront}:{product['id']}",
        "captured_at": "2026-09-07T12:00:00Z",
        "read_at": "2026-09-07T12:20:00Z",
        "products": [
            {
                "product_ref": str(product["id"]),
                "canonical_name": product.get("title") or "",
                "status": "active",
                "attributes": {},
                "offer": {
                    "price": float(variants[0].get("price") or 0.0) if variants else 0.0,
                    "currency": "USD",
                    "availability": (
                        "in_stock"
                        if any(variant.get("available") for variant in variants)
                        else "out_of_stock"
                    ),
                },
            }
        ],
    }


def _graded(text: str, snapshot: dict[str, Any], product_ref: str) -> list[dict[str, Any]]:
    """Every verdict this prose earns against this snapshot, zipped back by position."""
    claims = decompose_pitch(text, store_id="s", vocabulary=catalog_keys(snapshot, product_ref))
    if not claims:
        return []
    result = verify(
        {
            "pitch_id": product_ref,
            "store_id": "s",
            "product_ref": product_ref,
            "text": text,
            "claims": [dict(claim, claim_ref=str(index)) for index, claim in enumerate(claims)],
        },
        snapshot,
        VERIFIER_VERSION,
    )
    return list(result["claims"])


# ======================================================================================
# 1. The recorded corpus: 2,831 real merchant documents, and nobody is accused
# ======================================================================================
def test_the_recorded_corpus_is_the_size_it_was_measured_at() -> None:
    """A floor made of a corpus that quietly shrank is not a floor."""
    corpus = _recorded_corpus()
    assert len({storefront for storefront, _product, _prose in corpus}) == STOREFRONTS
    assert len(corpus) == DOCUMENTS_WITH_PROSE
    assert (
        sum(len(list(sentences(prose))) for _storefront, _product, prose in corpus)
        == SENTENCES_IN_THE_CORPUS
    )


def test_no_recorded_merchant_document_is_accused_of_contradicting_itself() -> None:
    """The false-positive floor, run over every recorded storefront.

    Each document's prose is graded against the platform's own crawl of that same document, so
    the two cannot honestly disagree about anything. Any ``contradicted`` here is the
    decomposer reading a claim the merchant did not make, and it is worth the published
    ``policy_penalties: -0.15`` plus a ``catalog_claim_accuracy`` hit to a real store.
    """
    accused: list[tuple[str, Any, Any, Any]] = []
    for storefront, product, prose in _recorded_corpus():
        snapshot = _snapshot_of(storefront, product)
        for row in _graded(prose, snapshot, str(product["id"])):
            if row["status"] == "contradicted":
                accused.append((storefront, product["id"], row["key"], row["claimed_value"]))
    assert accused == [], f"{len(accused)} real merchant documents were accused: {accused[:5]}"


def test_the_recorded_corpus_mints_only_what_is_true() -> None:
    """Every reading the corpus yields, named — and there is exactly one, and it is true.

    **The limitation of this corpus, stated rather than glossed.** These ten storefronts sell
    supplements, and no merchant among them writes "in stock", "out of stock", "sold out",
    "available now", "ready to ship" or "back-ordered" anywhere in 44,595 sentences. So the two
    stock rules — the ones D59 made decidable and therefore expensive to get wrong — are never
    exercised by it, and its silence is a floor rather than a discriminator. What grades those
    is the approved golden set below and the repository's own generators.
    """
    readings = [
        (storefront, claim["key"], claim["value"], claim["evidence"])
        for storefront, _product, prose in _recorded_corpus()
        for claim in decompose_pitch(prose, store_id="s", vocabulary=())
    ]
    assert readings == [THE_ONLY_TRUE_READING], readings


def test_no_real_availability_sentence_reaches_the_stock_rule() -> None:
    """The specific trap this corpus is best at, and the reason to keep it.

    "Available" is the word real merchants actually use, 289 distinct sentences of it, and
    almost none of them is about a shelf: "S-Acetyl L-Glutathione is a highly bio-available
    glutathione variant", "one of the most potent products available", "While there are many
    forms of magnesium available", "if there isn't enough oxygen available". The stock rule
    matches ``available now`` and not bare ``available`` for exactly this reason, and widening
    it — the obvious generalisation — lights up all 289 at once.
    """
    availability_sentences = {
        sentence
        for _storefront, _product, prose in _recorded_corpus()
        for _start, _end, sentence in sentences(prose)
        if re.search(r"availab", sentence, re.IGNORECASE)
    }
    assert len(availability_sentences) == AVAILABILITY_SENTENCES
    minted = {
        sentence: [claim["key"] for claim in decompose_pitch(sentence, vocabulary=("in_stock",))]
        for sentence in availability_sentences
    }
    assert {s: keys for s, keys in minted.items() if keys} == {}


# ======================================================================================
# 2. The approved answer key: both directions, decided by humans rather than by me
# ======================================================================================
@functools.lru_cache(maxsize=1)
def _golden() -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(GOLDEN_SET.read_text(encoding="utf-8"))["pitches"])


def _approved_pitches() -> list[Any]:
    return [pytest.param(pitch, id=str(pitch["pitch_id"])) for pitch in _golden()]


@pytest.mark.parametrize("pitch", _approved_pitches())
def test_every_approved_golden_pitch_agrees_with_its_answer_key(pitch: dict[str, Any]) -> None:
    """Thirteen human-approved pitches, decomposed and graded against their own snapshots.

    The answer key is the point: a human decided, per claim, whether the catalogue supports it,
    contradicts it or cannot decide it. So this gate cannot be satisfied by suppression — the
    key marks gp-001's "15 bar" a ``contradicted`` against a 9-bar catalogue and gp-001's
    "heat-exchange machine" a ``verified`` against the same snapshot, and the decomposer has to
    read the sentence well enough to earn both.

    Where the key has no row for a key the prose minted, the only requirement is that the
    verdict is not an ACCUSATION the humans did not make.
    """
    snapshot = pitch["catalog_snapshot"]
    approved = {claim["key"]: claim["expected_status"] for claim in pitch["claims"]}
    for row in _graded(pitch["text"], snapshot, str(pitch["product_ref"])):
        expected = approved.get(str(row["key"]))
        if expected is not None:
            assert row["status"] == expected, (
                f"{pitch['pitch_id']}: prose read {row['key']}={row['claimed_value']!r} as "
                f"{row['status']}, the approved answer key says {expected}"
            )
        else:
            assert row["status"] != "contradicted", (
                f"{pitch['pitch_id']}: prose accused the store on {row['key']}="
                f"{row['claimed_value']!r}, a key the approved answer key never graded"
            )


def test_the_answer_key_still_convicts_the_pitch_it_was_written_to_convict() -> None:
    """The adversarial half, named outright so the parametrised gate above cannot go vacuous.

    A decomposer that minted nothing at all would satisfy every assertion in this file except
    this one and :func:`test_the_recorded_corpus_mints_only_what_is_true`. gp-001 says "it
    pushes a full 15 bar of brew pressure" over a catalogue recording 9, and that verdict is
    the whole reason prose is decomposed at all.
    """
    gp001 = next(pitch for pitch in _golden() if pitch["pitch_id"] == "gp-001-espresso-mixed")
    verdicts = {
        str(row["key"]): (row["claimed_value"], row["status"])
        for row in _graded(gp001["text"], gp001["catalog_snapshot"], str(gp001["product_ref"]))
    }
    assert verdicts["pump_pressure_bar"] == (15.0, "contradicted")
    assert verdicts["boiler_type"] == ("heat exchange", "verified")


# ======================================================================================
# 3. The traffic this decomposer actually meets: this repository's own generators
# ======================================================================================
#: Prose this repository SHIPS or GENERATES, verbatim, with the file it came from. The reading
#: beside each one is what the decomposer takes out of it, and every one of these is unchanged
#: by the guard rewrite — the point of the list is that closing the false positives cost the
#: honest path nothing.
GENERATED_PROSE: tuple[tuple[str, str, list[tuple[str, Any]]], ...] = (
    (
        "packages/llm/fixtures/recorded/store_agent_pitch.json",
        "It leaves us within two business days, so it can be on your doorstep before the "
        "weekend. If the fit is wrong, returns are free for thirty days. Merino wool, and in "
        "stock now.",
        [("in_stock", True)],
    ),
    (
        "deploy/demo-seed/merchant-dashboard/dashboard-page.json (a served Bid.message)",
        "Free returns: 30 day return window. Also: in stock: yes; units left: 9.",
        [("in_stock", True)],
    ),
    (
        "store_agent.runtime.pitch.fallback_pitch over fixtures/envelopes/store-beta.approved",
        "You weighted warranty, and here it is: 1 year. Also: material: down; in stock: yes.",
        [("warranty_months", 12), ("in_stock", True)],
    ),
    (
        "store_agent.runtime.pitch.fallback_pitch over deploy/demo/store-contexts/toniiq.com",
        "You weighted brand, and here it is: Toniiq - Elevated Nutrients. Also: free returns: "
        "30 return window; in stock: yes.",
        [("in_stock", True)],
    ),
    (
        "store_agent.runtime.pitch.fallback_pitch over deploy/demo/store-contexts/"
        "oregonswildharvest.com",
        "You weighted in stock, and here it is: yes. Also: brand: Oregon's Wild Harvest; free "
        "returns: 30 return window.",
        [("in_stock", True)],
    ),
    (
        "apps/seller-reference persona 'honest'",
        "unit price: 19.50. dispatch window: ships within 2 business days. return policy: free "
        "returns for 30 days. ingredients: arabica coffee beans.",
        [("dispatch_window", 2), ("return_window_days", 30)],
    ),
    (
        "apps/seller-reference persona 'aggressive'",
        "unit price: 18.50. discount: 25%. dispatch window: ships within 24 hours. return "
        "policy: free returns for 60 days. ingredients: 100% single-origin arabica.",
        [("return_window_days", 60)],
    ),
    (
        "fixtures/pages/store-example/warranty.html",
        "Every pack carries a 3 year manufacturing warranty. Orders ship within 1 business day.",
        [("warranty_months", 36), ("dispatch_window", 1)],
    ),
    (
        "fixtures/pages/store-example/shipping.html",
        "We ship every order within 2 business days from our Portland warehouse. Returns are "
        "accepted within 30 days of delivery for unworn items. All footwear carries a 1 year "
        "manufacturing guarantee.",
        [("dispatch_window", 2), ("return_window_days", 30), ("warranty_months", 12)],
    ),
    (
        "fixtures/pages/store-example/returns.html",
        "Returns are accepted within 45 days of delivery. Return shipping is free for orders "
        "over $75.",
        [("return_window_days", 45)],
    ),
    (
        "packages/llm/fixtures/recorded/store_agent_pitch.json (the longer reply)",
        "Merino wool is the whole of it here: it holds warmth on a cold ride and stays wearable "
        "at the other end. It leaves us within two business days, and if it turns out not to be "
        "your commute layer, returns are free for thirty days.",
        [],
    ),
)


@pytest.mark.parametrize(("source", "text", "expected"), GENERATED_PROSE)
def test_the_repos_own_generated_prose_reads_exactly_as_it_did(
    source: str, text: str, expected: list[tuple[str, Any]]
) -> None:
    """The honest path, priced. Every one of these is prose this tree ships or renders.

    The last entry mints NOTHING and is here on purpose: "returns are free for thirty days" and
    "it leaves us within two business days" are real commitments in a reviewed model reply that
    the rule table cannot read, because it wants digits and a shipping verb. That is a false
    NEGATIVE, it is free to the seller, and it is the direction this module errs in by design.
    """
    vocabulary = (
        "in_stock",
        "warranty_months",
        "return_window_days",
        "dispatch_window",
        "boiler_type",
        "pump_pressure_bar",
        "water_tank_l",
        "voltage",
        "units_left",
    )
    minted = [
        (claim["key"], claim["value"])
        for claim in decompose_pitch(text, store_id="s", vocabulary=vocabulary)
    ]
    assert minted == expected, source
