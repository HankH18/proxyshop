"""T-080 verify — the seed generator, and `make demo-seed` against a REAL shopify-stub.

The ticket's verify command is ``pytest fixtures/tests/test_seed.py -q``, so the load-bearing
claims live here:

* the generator is pure and deterministic under a fixed seed, and actually *uses* the seed;
* it seeds the manifest's dishonest store among N stores from the SEED_CATEGORY config;
* ``apply()`` is idempotent — the second application creates nothing;
* **and the same idempotence holds over HTTP against a real running shopify-stub**, driven
  through ``python -m fixtures.seed``, which is literally the command ``make demo-seed``
  runs. That last test is the one that stops this file from being a green that only proves
  a stub was called the way the stub expects: it starts the real ASGI app on a real
  ephemeral port, runs the real CLI twice, and then reads the seeded variant back out
  through the stub's own cart-permalink route.
"""

from __future__ import annotations

import json
import re

import pytest

from fixtures import FIXTURES_DIR
from fixtures.generator import GeneratorError, apply, generate
from fixtures.manifest import (
    CATALOG_DIM,
    TRUST_DIMENSIONS,
    UnmappedClaimTypeError,
    claim_dimension,
    load_golden_set,
    load_manifest,
    validate_claim_types,
)
from fixtures.seed import build_payload

MANIFEST = load_manifest()
SEED_CATEGORY = str(MANIFEST["seed_category"])
SEED = int(MANIFEST["seed"])
DISHONEST_ID = str(MANIFEST["dishonest_store"]["store_id"])


def _canonical(payload: object) -> str:
    """The frozen suite's canonicaliser: byte identity, with no fallback encoder."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=None)


# ---------------------------------------------------------------------------------
# The manifest is loadable ground truth, and the D53 guard actually refuses something
# ---------------------------------------------------------------------------------
def test_manifest_loads_and_scripts_all_six_trust_dimensions() -> None:
    dims = {b["dim"] for b in MANIFEST["dishonest_store"]["behaviours"]}
    assert dims <= TRUST_DIMENSIONS
    assert CATALOG_DIM in dims, (
        "S2's dishonest store must lie about the goods, not only about the deal — otherwise "
        "the sixth dimension is never exercised end to end"
    )
    assert dims == TRUST_DIMENSIONS, (
        "the scripted behaviours leave a trust dimension unexercised: "
        f"{sorted(TRUST_DIMENSIONS - dims)}"
    )


def test_an_unmapped_claim_type_raises_instead_of_defaulting_to_a_dimension() -> None:
    """D53 / T-080 acceptance 4 — the guard refuses an input nothing else would catch.

    The input below is accepted by every other check in the repo: it is valid JSON, it names
    a real dimension for every type it *does* map, and the claim it carries is well-formed.
    Only the exhaustiveness guard rejects it — and rejecting it is the point, because a
    default would silently park a product-fact contradiction on whatever dimension the
    default named.
    """
    doctored = dict(MANIFEST)
    doctored["claim_type_dimensions"] = {
        k: v for k, v in MANIFEST["claim_type_dimensions"].items() if k != "nutrition"
    }

    with pytest.raises(UnmappedClaimTypeError) as caught:
        validate_claim_types(doctored, ["unit_price", "nutrition"])
    assert "nutrition" in str(caught.value)
    assert "never defaults" in str(caught.value)

    # The same input under the real table is fine, so the guard is discriminating rather
    # than merely loud.
    validate_claim_types(MANIFEST, ["unit_price", "nutrition"])
    assert claim_dimension(MANIFEST, "nutrition") == CATALOG_DIM

    with pytest.raises(UnmappedClaimTypeError):
        claim_dimension(MANIFEST, "vibes")


def test_every_golden_claim_type_has_a_route_into_trust() -> None:
    golden = load_golden_set(MANIFEST)
    table = MANIFEST["claim_type_dimensions"]
    for pitch in golden["pitches"]:
        for claim in pitch["claims"]:
            assert claim["claim_type"] in table, (
                f"{pitch['pitch_id']}#{claim['claim_ref']} has claim_type "
                f"{claim['claim_type']!r}, which the approved table does not map"
            )


# ---------------------------------------------------------------------------------
# C9 — the generator is deterministic, seeded, and covers the roster
# ---------------------------------------------------------------------------------
def test_generate_is_byte_identical_under_a_fixed_seed() -> None:
    first = generate(SEED_CATEGORY, SEED)
    second = generate(SEED_CATEGORY, SEED)
    assert _canonical(first) == _canonical(second)


def test_generate_actually_uses_its_seed() -> None:
    assert _canonical(generate(SEED_CATEGORY, SEED)) != _canonical(
        generate(SEED_CATEGORY, SEED + 1)
    ), "a generator that ignores its seed is not seeded"


def test_generate_seeds_n_stores_including_the_manifests_dishonest_one() -> None:
    payload = generate(SEED_CATEGORY, SEED)
    for key in ("catalog", "stores", "event_script", "variants"):
        assert payload[key], f"generate(...)[{key!r}] must not be empty"

    store_ids = [s["store_id"] for s in payload["stores"]]
    assert len(store_ids) == len(MANIFEST["stores"]) >= 2
    assert DISHONEST_ID in store_ids, (
        "T-080 acceptance 2: the generator must seed the manifest's dishonest store"
    )
    assert [s["store_id"] for s in MANIFEST["stores"]] == store_ids, (
        "the roster is the manifest's, in the manifest's order — not the generator's idea"
    )

    config_path = FIXTURES_DIR / "catalog" / f"{SEED_CATEGORY}.json"
    products_per_store = json.loads(config_path.read_text(encoding="utf-8"))["products_per_store"]
    assert len(payload["catalog"]) == products_per_store * len(store_ids), (
        "the catalog size must come from the SEED_CATEGORY config, so the generator is "
        "parameterised rather than hard-coded"
    )


def test_generated_payload_is_plain_json_data() -> None:
    """`default=None` installs no fallback encoder, so a dataclass anywhere raises."""
    payload = generate(SEED_CATEGORY, SEED)
    assert json.loads(_canonical(payload)) == payload


def test_generate_refuses_an_unknown_category_and_a_non_integer_seed() -> None:
    with pytest.raises(GeneratorError):
        generate("no-such-category", SEED)
    with pytest.raises(GeneratorError):
        generate("../etc/passwd", SEED)
    with pytest.raises(GeneratorError):
        generate(SEED_CATEGORY, "20260901")  # type: ignore[arg-type]


def test_event_script_replays_the_manifest_behaviours_once_per_episode() -> None:
    payload = generate(SEED_CATEGORY, SEED)
    behaviours = MANIFEST["dishonest_store"]["behaviours"]
    budget = MANIFEST["episode_budget"]
    script = payload["event_script"]
    assert len(script) == len(behaviours) * budget
    assert [r["kind"] for r in script[: len(behaviours)]] == [b["kind"] for b in behaviours]
    assert [r["episode"] for r in script] == sorted(r["episode"] for r in script)
    assert {r["store_id"] for r in script} == {DISHONEST_ID}


# ---------------------------------------------------------------------------------
# T-080 acceptance 3 — idempotence, offline and then over real HTTP
# ---------------------------------------------------------------------------------
def test_apply_is_idempotent_and_mutates_its_target_in_place() -> None:
    payload = generate(SEED_CATEGORY, SEED)
    target: dict = {}

    first = apply(payload, target)
    assert first["created"] > 0
    assert first["unchanged"] == 0
    assert len(target) == first["created"], "apply() must mutate the target in place"

    second = apply(payload, target)
    assert second["created"] == 0, "applying the same payload twice must be a no-op"
    assert second["unchanged"] == first["created"]
    assert second["updated"] == 0


def test_apply_reports_an_update_when_the_stored_record_differs() -> None:
    """Idempotence must be 'equal records are unchanged', not 'anything present is skipped'."""
    payload = generate(SEED_CATEGORY, SEED)
    target: dict = {}
    apply(payload, target)
    key = next(k for k in target if k.startswith("variant:"))
    target[key] = {**target[key], "price": "0.01"}

    report = apply(payload, target)
    assert report["updated"] == 1, "a drifted record must be corrected, not reported unchanged"
    assert report["created"] == 0
    assert target[key]["price"] != "0.01"


@pytest.mark.timeout(120)
def test_make_demo_seed_is_idempotent_against_a_real_shopify_stub(capsys) -> None:
    """The real path: the real CLI, over real HTTP, against the real stub app.

    ``make demo-seed`` is ``python -m fixtures.seed --category "$(SEED_CATEGORY)"``. This
    test runs that module's ``main`` twice against a shopify-stub started on an ephemeral
    port, and then reads a seeded variant back out through the stub's own cart-permalink
    route — the route a checkout link actually resolves against. The green is evidence that
    the seeder puts variants into a real stub's catalog and that a second run changes
    nothing, not that a fake accepted a call.
    """
    httpx = pytest.importorskip("httpx")
    try:
        from shopify_stub.app import create_app

        from proxyshop_support.asgi_server import serve
    except ImportError as exc:  # pragma: no cover - the stub is merged; this is a guard
        pytest.skip(f"shopify-stub is not importable: {exc}")

    from fixtures.seed.__main__ import main

    with serve(create_app()) as base_url:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            argv = ["--category", SEED_CATEGORY, "--stub-url", base_url]

            assert main(argv, client=client) == 0
            first = json.loads(capsys.readouterr().out)
            assert first["mode"] == "stub"
            assert first["created"] == first["variants"] > 0
            assert first["unchanged"] == 0

            assert main(argv, client=client) == 0
            second = json.loads(capsys.readouterr().out)
            assert second["created"] == 0, (
                "T-080 acceptance 3: `make demo-seed` run twice against the stub must be a "
                f"no-op the second time, got {second}"
            )
            assert second["unchanged"] == first["created"]

            # Read one seeded variant back through Shopify's own surface: the cart
            # permalink resolves by variant id and 303s to checkout only if the variant is
            # really in the stub's catalog.
            payload = build_payload(SEED_CATEGORY, SEED)
            variant_id = payload["variants"][0]["variant_id"]
            response = client.get(f"{base_url}/cart/{variant_id}:1")
            assert response.status_code == 303, (
                f"the stub does not resolve seeded variant {variant_id}: "
                f"{response.status_code} {response.text[:200]}"
            )
            assert "/checkouts/" in response.headers["location"]

            missing = client.get(f"{base_url}/cart/{variant_id + 10**9}:1")
            assert missing.status_code == 404, (
                "the read-back must be able to fail: an unseeded variant id has to 404, or "
                "the 303 above proves nothing about the seed"
            )


def test_dry_run_touches_no_service(capsys) -> None:
    from fixtures.seed.__main__ import main

    assert main(["--category", SEED_CATEGORY, "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["dishonest_store_id"] == DISHONEST_ID
    assert report["seed"] == SEED


def test_demo_seed_reports_a_diagnosis_rather_than_a_traceback_when_no_stub_is_running(
    capsys,
) -> None:
    from fixtures.seed.__main__ import main

    class _Refuses:
        def post(self, url: str, *, json: object = None) -> object:
            raise ConnectionError("connection refused")

    assert (
        main(["--stub-url", "http://127.0.0.1:1", "--category", SEED_CATEGORY], client=_Refuses())
        == 2
    )
    err = capsys.readouterr().err
    assert "could not reach the shopify-stub" in err
    assert re.search(r"--dry-run", err)
