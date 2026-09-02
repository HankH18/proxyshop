"""The parameterised ``SEED_CATEGORY`` catalog generator (T-080 acceptance 2 and 3).

Two pure functions, and the split between them is the whole design:

``generate(seed_category, seed)``
    Reads the category config (``fixtures/catalog/<seed_category>.json``) and the approved
    manifest's store roster, and returns a plain-JSON payload describing the world to seed:
    stores, a catalog, the stub-shaped variant rows, and the dishonest store's event script.
    It touches no network and no service, and under a fixed seed it is byte-identical run to
    run (C9: fixed seeds reproduce identical streams).

``apply(payload, target)``
    Upserts that payload into ``target`` **in place** and reports ``{created, unchanged,
    updated}``. The second application of the same payload creates nothing — which is what
    ``make demo-seed`` idempotence means, expressed at a surface that needs no service
    running. The report shape and the upsert rule are deliberately identical to the
    shopify-stub's own ``POST /_stub/seed``, so the offline assertion and the over-HTTP
    behaviour are the same contract rather than two similar ones.

Store roster ground truth
-------------------------
Which stores exist, and which one is dishonest, come from the **manifest** — not from the
category config. A catalog knob deciding who the liar is would put ground truth back in the
generator, which is exactly what SPEC A3 forbids. The category config is therefore a pure
product-shape document with no store roster in it at all.
"""

from __future__ import annotations

__all__ = ["CATALOG_DIR", "GeneratorError", "apply", "category_config_path", "generate"]

import hashlib
import json
import pathlib
import random
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fixtures import FIXTURES_DIR
from fixtures.manifest import load_manifest

#: Where a ``SEED_CATEGORY`` config lives: ``fixtures/catalog/<seed_category>.json``.
CATALOG_DIR = FIXTURES_DIR / "catalog"

#: Id bases chosen at realistic Shopify magnitudes, so a consumer that assumes 32-bit ids
#: fails here rather than in production.
_VARIANT_ID_BASE = 44_000_000_000_001
_PRODUCT_ID_BASE = 77_000_000_000_001

_CENT = Decimal("0.01")


class GeneratorError(Exception):
    """The seed generator cannot produce a payload from the inputs it was given."""


def category_config_path(seed_category: str) -> pathlib.Path:
    """The pinned path of a category config. No search, no fallback."""
    if not isinstance(seed_category, str) or not seed_category.strip():
        raise GeneratorError("seed_category must be a non-empty string")
    if "/" in seed_category or seed_category.startswith("."):
        raise GeneratorError(f"seed_category {seed_category!r} is not a bare category name")
    return CATALOG_DIR / f"{seed_category}.json"


def _load_category(seed_category: str) -> dict[str, Any]:
    path = category_config_path(seed_category)
    if not path.is_file():
        raise GeneratorError(
            f"no SEED_CATEGORY config for {seed_category!r} at "
            f"{path.relative_to(FIXTURES_DIR.parent).as_posix()}"
        )
    config = json.loads(path.read_text(encoding="utf-8"))
    families = config.get("product_families")
    if not isinstance(families, list) or not families:
        raise GeneratorError(f"{path} declares no product_families")
    return config


def _rng(seed_category: str, seed: int) -> random.Random:
    """A deterministic RNG keyed on both inputs.

    Derived through sha256 rather than seeding on the raw string: `Random(str)` is stable in
    practice, but an explicit integer derivation makes "byte-identical across two calls" a
    property of this module rather than of CPython's string-seeding internals.
    """
    digest = hashlib.sha256(f"{seed_category}:{seed}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _money(cents: int) -> str:
    """Money as a fixed 2-place string — never a float. JSON floats do not round-trip cents."""
    return str((Decimal(cents) / 100).quantize(_CENT, rounding=ROUND_HALF_UP))


def _attribute(rng: random.Random, spec: Any) -> Any:
    """One generated attribute value from a category config's attribute template."""
    if not isinstance(spec, dict):
        return spec
    if "fixed" in spec:
        return spec["fixed"]
    if "choices" in spec:
        return rng.choice(list(spec["choices"]))
    if "range" in spec:
        low, high = spec["range"]
        return rng.randint(int(low), int(high))
    raise GeneratorError(f"attribute spec {spec!r} declares none of fixed/choices/range")


def _slug(text: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def generate(seed_category: str, seed: int) -> dict[str, Any]:
    """Build the seed payload for one ``(SEED_CATEGORY, seed)`` pair.

    Returns plain JSON data — no dataclasses, no Decimals, no datetimes — because the
    payload is compared for byte identity by canonical JSON, and a non-serializable value
    anywhere inside it must fail loudly rather than compare equal by accident.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise GeneratorError(f"seed must be an integer, got {type(seed).__name__}")

    config = _load_category(seed_category)
    manifest = load_manifest()
    rng = _rng(seed_category, seed)

    roster = manifest.get("stores") or []
    dishonest_id = str(manifest["dishonest_store"]["store_id"])
    if not roster:
        raise GeneratorError("manifest.stores is empty: there is no roster to seed")
    if dishonest_id not in {str(s.get("store_id")) for s in roster}:
        raise GeneratorError(
            f"manifest.dishonest_store.store_id {dishonest_id!r} is not in manifest.stores"
        )

    families = config["product_families"]
    per_store = int(config.get("products_per_store", len(families)))
    currency = str(config.get("currency", "USD"))

    stores: list[dict[str, Any]] = []
    catalog: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    counter = 0

    for store in roster:
        store_id = str(store["store_id"])
        product_refs: list[str] = []
        for slot in range(per_store):
            family = families[slot % len(families)]
            name = rng.choice(list(family["canonical_names"]))
            low, high = family["price_band_cents"]
            base_cents = rng.randint(int(low), int(high))
            attributes = {
                key: _attribute(rng, spec)
                for key, spec in sorted((family.get("attribute_template") or {}).items())
            }
            product_ref = f"prod-{_slug(store_id)}-{_slug(family['family'])}-{slot}"
            product_id = _PRODUCT_ID_BASE + counter
            product_refs.append(product_ref)
            product_variants: list[dict[str, Any]] = []
            for variant_index, variant in enumerate(family.get("variants") or [{"suffix": ""}]):
                multiplier = Decimal(str(variant.get("price_multiplier", 1.0)))
                cents = int(
                    (Decimal(base_cents) * multiplier).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                suffix = str(variant.get("suffix", "")).strip()
                title = f"{name} - {suffix}" if suffix else name
                variant_id = _VARIANT_ID_BASE + counter
                sku = f"{_slug(store_id)[:6].upper()}-{_slug(name)[:12].upper()}-{variant_index}"
                row = {
                    "variant_id": variant_id,
                    "product_id": product_id,
                    "title": title,
                    "price": _money(cents),
                    "currency": currency,
                    "sku": sku,
                    "available": True,
                }
                variants.append(row)
                product_variants.append(
                    {
                        "variant_ref": f"var-{_slug(store_id)}-{_slug(family['family'])}"
                        f"-{slot}-{variant_index}",
                        "variant_id": variant_id,
                        "title": title,
                        "price": _money(cents),
                        "grams": variant.get("grams"),
                    }
                )
                counter += 1
            catalog.append(
                {
                    "product_ref": product_ref,
                    "product_id": product_id,
                    "store_id": store_id,
                    "family": family["family"],
                    "canonical_name": name,
                    "currency": currency,
                    "attributes": attributes,
                    "variants": product_variants,
                }
            )
        stores.append(
            {
                "store_id": store_id,
                "business_identity": store.get("business_identity"),
                "display_name": store.get("display_name"),
                "domain": store.get("domain"),
                "honest": bool(store.get("honest", True)),
                "product_refs": product_refs,
            }
        )

    event_script = _event_script(manifest)

    return {
        "seed_category": seed_category,
        "seed": seed,
        "config_version": config.get("config_version"),
        "currency": currency,
        "dishonest_store_id": dishonest_id,
        "stores": stores,
        "catalog": catalog,
        "variants": variants,
        "event_script": event_script,
    }


def _event_script(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Every scripted dishonest behaviour, replayed once per episode of the budget.

    The manifest is ground truth: the order and the ``kind`` spellings come from it
    unchanged, so a reader can compare this script to the manifest element for element.
    """
    store = manifest["dishonest_store"]
    store_id = str(store["store_id"])
    behaviours = list(store["behaviours"])
    budget = int(manifest["episode_budget"])
    script: list[dict[str, Any]] = []
    for episode in range(1, budget + 1):
        for index, behaviour in enumerate(behaviours):
            script.append(
                {
                    "episode": episode,
                    "sequence": index,
                    "store_id": store_id,
                    "kind": str(behaviour["kind"]),
                    "dim": str(behaviour["dim"]),
                    "type": str(behaviour["type"]),
                    "claim_type": behaviour.get("claim_type"),
                }
            )
    return script


def _records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The payload as an addressable ``{key: record}`` map — the unit of idempotence."""
    records: dict[str, dict[str, Any]] = {}
    for store in payload.get("stores") or []:
        records[f"store:{store['store_id']}"] = store
    for product in payload.get("catalog") or []:
        records[f"product:{product['product_ref']}"] = product
    for variant in payload.get("variants") or []:
        records[f"variant:{variant['variant_id']}"] = variant
    if not records:
        raise GeneratorError("the payload carries nothing to apply")
    return records


def apply(payload: dict[str, Any], target: dict[str, Any]) -> dict[str, int]:
    """Idempotently upsert ``payload`` into ``target``, **mutating it in place**.

    Returns ``{"created", "unchanged", "updated", "total"}`` — the same report shape and the
    same rule the shopify-stub's ``POST /_stub/seed`` uses: a record whose stored value is
    already equal to the incoming one counts as *unchanged* and is not rewritten.

    Applying the same payload to the same target twice therefore reports ``created == 0``
    the second time, with every previously-created record reported as ``unchanged``. That is
    ``make demo-seed`` idempotence (T-080 acceptance 3) at a surface that needs no service.
    """
    if not isinstance(target, dict):
        raise GeneratorError(f"apply() target must be a dict, got {type(target).__name__}")
    records = _records(payload)
    created = unchanged = updated = 0
    for key, record in records.items():
        existing = target.get(key)
        if existing is None:
            created += 1
        elif existing == record:
            unchanged += 1
            continue
        else:
            updated += 1
        target[key] = json.loads(json.dumps(record))
    return {
        "created": created,
        "unchanged": unchanged,
        "updated": updated,
        "total": len(records),
    }
