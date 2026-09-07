"""The new refusal, driven with THIS REPO'S OWN fixtures — both directions.

A screen is only half-tested by the copy it refuses. The other half, and the one that costs a
shopper something when it is wrong, is the copy it must NOT refuse: a numeric-grounding rule
that rejected honest prose would silently downgrade every slot on every shortlist to the
deterministic assembly, and every test asserting "a case was served" would stay green while the
product got worse.

So this file drives the corpus the repo already ships:

``fixtures/dialogues/*.json``
    five real buyer dialogues, each carrying the ``expected_intent`` the clarifier produces —
    real queries, real R19 hard constraints and preferences, real budget bands including
    ``unspecified``.
``fixtures.generator.generate("coffee", seed)``
    the parameterised catalogue: real stores (honest and dishonest), real products, real
    variant prices, real attributes. Shortlist slots are built from those bytes rather than
    invented here, so the material a case is made from is the material the rest of the system
    would have.

Every pairing is driven through the **served route**, and then:

* every slot the platform holds facts about gets a case (nothing is silently dropped);
* every ASSEMBLED case survives :func:`~buyer_svc.pitch.screen_reasons` unchanged — the
  platform's own copy is not something its own screen would refuse;
* every number in every served case is one the platform already held for that slot;
* and the same corpus, with one number changed, IS refused — the positive control, without
  which "honest traffic passes" is a claim about a screen that might not be on.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fixtures import generator

RENDER = "/buyer/shortlist/render"
DIALOGUES = Path(__file__).resolve().parents[4] / "fixtures" / "dialogues"
SEED = 7


@pytest.fixture()
def client():
    main = importlib.import_module("buyer_svc.main")
    with TestClient(main.create_app()) as test_client:
        yield test_client


def _intents() -> list[tuple[str, dict[str, Any]]]:
    """Every shipped dialogue's structured intent, by fixture name."""
    found = []
    for path in sorted(DIALOGUES.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        intent = document.get("expected_intent")
        if isinstance(intent, dict):
            found.append((path.stem, intent))
    return found


def _claim(key: str, value: Any, source: str, unit: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "provenance": {
            "source": source,
            "ref": f"ref://{key}",
            "observed_at": "2026-09-06T00:00:00Z",
            "authority_rank": 1 if source == "owner_statement" else 3,
        },
    }


def _slots() -> list[dict[str, Any]]:
    """Shortlist slots built from the generated catalogue's own bytes.

    An honest store is modelled as in-network (``owner_statement`` provenance, and its own
    advocate's message on the bid); a dishonest one as scraped, with no advocate — which is
    the case the buyer-side agent exists for.
    """
    payload = generator.generate("coffee", SEED)
    by_ref = {product["product_ref"]: product for product in payload["catalog"]}
    slots: list[dict[str, Any]] = []
    names = ["fit", "value", "reliability", "specialist"]
    for index, store in enumerate(payload["stores"][:4]):
        product = by_ref[store["product_refs"][0]]
        variant = product["variants"][0]
        in_network = bool(store.get("honest"))
        source = "owner_statement" if in_network else "scraped"
        attributes = product["attributes"]
        commitments = [
            _claim("origin", attributes["origin"], source),
            _claim("roast_level", attributes["roast_level"], source),
            _claim(
                "caffeine_mg_per_serving",
                attributes["caffeine_mg_per_serving"],
                source,
                unit="mg",
            ),
        ]
        certifications = attributes.get("certifications") or []
        if certifications:
            commitments.append(_claim("certifications", ", ".join(certifications), source))
        slot: dict[str, Any] = {
            "slot": names[index],
            "bid_ref": f"auc-honest:{store['store_id']}",
            "fit_score": round(0.9 - index * 0.1, 2),
            "trust_summary": {
                "store_id": store["store_id"],
                "available": True,
                "score": 0.88 if in_network else 0.42,
                "low_data": not in_network,
            },
            "provenance_labels": ["store-confirmed" if in_network else "from their website"],
            "product": {
                "product_ref": product["product_ref"],
                "variant_ref": variant["variant_ref"],
            },
            "price": {
                "unit_price": float(variant["price"]),
                "total_price": float(variant["price"]),
                "currency": product["currency"],
                "discount": None,
                "expires_at": None,
            },
            "commitments": commitments,
            "store_domain": store["domain"],
        }
        if in_network:
            slot["message"] = (
                f"{store['display_name']} roasts to order and ships the same week; "
                f"the {product['canonical_name']} is what we drink ourselves."
            )
        slots.append(slot)
    return slots


CORPUS_SLOTS = _slots()
CORPUS_INTENTS = _intents()


def test_the_corpus_is_the_repositorys_own_and_is_not_empty():
    """If this ever reads zero, every assertion below is vacuously true."""
    assert len(CORPUS_INTENTS) >= 5, CORPUS_INTENTS
    assert len(CORPUS_SLOTS) == 4
    assert any("message" in slot for slot in CORPUS_SLOTS), "no in-network store in the corpus"
    assert any("message" not in slot for slot in CORPUS_SLOTS), "no scraped store in the corpus"


@pytest.mark.parametrize("name,intent", CORPUS_INTENTS, ids=[n for n, _ in CORPUS_INTENTS])
def test_every_shipped_dialogue_gets_a_case_for_every_candidate(client, name, intent):
    response = client.post(
        RENDER,
        json={
            "shortlist": {"auction_id": "auc-honest", "slots": CORPUS_SLOTS},
            "intent": intent,
        },
    )
    assert response.status_code == 200, response.text
    served = response.json()["slots"]
    assert len(served) == len(CORPUS_SLOTS)
    for slot in served:
        pitch = slot["pitch"]
        assert pitch is not None, f"{name}: {slot['bid_ref']} had nobody making its case"
        assert pitch["platform_case"].strip()
        assert pitch["facts"]


@pytest.mark.parametrize("name,intent", CORPUS_INTENTS, ids=[n for n, _ in CORPUS_INTENTS])
def test_the_platforms_own_copy_is_never_refused_by_the_platforms_own_screen(name, intent):
    """The false-positive direction. An assembled case that its own screen would reject is a
    screen that will reject the model's honest prose too, and nothing would say so."""
    labels = importlib.import_module("buyer_svc.accept.labels")
    pitch = importlib.import_module("buyer_svc.pitch")

    shortlist = {"auction_id": "auc-honest", "slots": CORPUS_SLOTS}
    for slot in labels.render_shortlist(shortlist):
        material = pitch.material_for(slot, intent=intent, profile=None)
        assembled = pitch.assemble(material)
        assert assembled.strip(), f"{name}: {material.bid_ref} assembled to nothing"
        reasons = pitch.screen_reasons(assembled, material)
        assert reasons == (), f"{name}: {material.bid_ref} refused its own copy: {reasons}"


@pytest.mark.parametrize("name,intent", CORPUS_INTENTS, ids=[n for n, _ in CORPUS_INTENTS])
def test_the_screen_still_bites_on_the_same_corpus(name, intent):
    """The positive control. One invented number in otherwise-honest prose, over the same real
    material, and the screen refuses it — so "honest traffic passes" is not "the screen is off".
    """
    labels = importlib.import_module("buyer_svc.accept.labels")
    pitch = importlib.import_module("buyer_svc.pitch")

    shortlist = {"auction_id": "auc-honest", "slots": CORPUS_SLOTS}
    for slot in labels.render_shortlist(shortlist):
        material = pitch.material_for(slot, intent=intent, profile=None)
        honest = pitch.assemble(material)
        fabricated = f"{honest} It also ships in 3 days with a 5 year warranty."
        reasons = pitch.screen_reasons(fabricated, material)
        assert any(reason.startswith("invented numbers") for reason in reasons), (
            f"{name}: {material.bid_ref} accepted a number the platform never held: {reasons}"
        )


@pytest.mark.parametrize("name,intent", CORPUS_INTENTS, ids=[n for n, _ in CORPUS_INTENTS])
def test_every_number_a_shopper_reads_is_one_the_platform_held(client, name, intent):
    """End to end, on the served bytes: no case quotes a figure that is not in its own facts."""
    pitch_module = importlib.import_module("buyer_svc.pitch")
    response = client.post(
        RENDER,
        json={
            "shortlist": {"auction_id": "auc-honest", "slots": CORPUS_SLOTS},
            "intent": intent,
        },
    )
    assert response.status_code == 200, response.text
    for slot in response.json()["slots"]:
        served = slot["pitch"]
        held: set[str] = set()
        for fact in served["facts"]:
            held |= pitch_module.numeric_tokens(fact["value"])
        quoted = pitch_module.numeric_tokens(served["platform_case"])
        assert quoted <= held, f"{name}: {slot['bid_ref']} quoted {sorted(quoted - held)}"


def test_the_corpus_renders_byte_identically_twice(client):
    body = {
        "shortlist": {"auction_id": "auc-honest", "slots": CORPUS_SLOTS},
        "intent": CORPUS_INTENTS[0][1],
    }
    assert client.post(RENDER, json=body).content == client.post(RENDER, json=body).content


def test_the_scraped_stores_low_data_caveat_is_shown_and_never_led_with(client):
    """The organic result is the platform's own voice, so it may not hide the bad half — and
    an advocate that opened with the bad half would be advocacy nobody asked for."""
    response = client.post(
        RENDER,
        json={
            "shortlist": {"auction_id": "auc-honest", "slots": CORPUS_SLOTS},
            "intent": CORPUS_INTENTS[0][1],
        },
    )
    assert response.status_code == 200, response.text
    scraped = [
        slot
        for slot, source in zip(response.json()["slots"], CORPUS_SLOTS, strict=True)
        if "message" not in source
    ]
    assert scraped, "the corpus lost its scraped stores"
    for slot in scraped:
        keys = [fact["key"] for fact in slot["pitch"]["facts"]]
        assert "observations" in keys, slot["pitch"]
        assert keys[0] != "observations", slot["pitch"]
        assert "few so far" not in slot["pitch"]["platform_case"]
