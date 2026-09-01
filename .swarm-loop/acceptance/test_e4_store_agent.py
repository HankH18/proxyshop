"""Epic E4 — Store agent & sellers. FROZEN acceptance criteria.

Covers the promised behaviour of the hosted store advocate and the unharnessed
reference personas:

* R7  — shadow mode computes and logs bids but submits nothing; activation flips
        submission on without a restart, and the kill switch flips it off again.
* R8  — every claim and every discount a hosted agent emits arrives through a
        provenance-tagged tool hook; envelope walls (floor / max discount) are hard;
        a claim assembled outside the hooks is refused at the boundary; external
        bids enter signed and are admitted as *unverified* rather than trusted.
* R10 — the cold agent (no learned policy) emits a deterministic default bid at
        list price with the envelope's standing commitments and no improvisation.
* R13 — an injected TrustEventPayload deterministically changes the next bid's
        posture/rationale.
* R17 — the store loop learns from its OWN outcomes only; the network prior builder
        never reads discount fields; two stores learn independently.
* R18 / S8 / A3 — the aggressive reference persona emits exactly the claims the
        human-approved fixture manifest scripts, all tagged `seller_asserted`.
* S4  — the store-side half of the seeded-determinism criterion.
* S5  — the hosted-trace half: every claim in an emitted bid traces to a hook call.

Authoring rules (see `.swarm-loop/acceptance/README.md`), both binding:

1. Product code is imported INSIDE the test body, never at module scope, so an
   unbuilt goal is a clean per-test failure instead of a collection error.
2. Every test carries `@pytest.mark.epic("E4")` and `@pytest.mark.ticket(...)`.

Offline by construction: no network, no LLM, no Shopify, no database, no container,
no wall-clock assertion, no unseeded randomness. Module scope imports stdlib and
pytest only.
"""
from __future__ import annotations

import importlib.machinery
import json
import pathlib
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Import bootstrap for the two hyphenated source trees.
#
# `packages/store-agent` and `apps/seller-reference` are not valid dotted names.
# The frozen conftest is expected to register aliases for them, but this file
# registers them itself as well: the registration is idempotent, and a frozen
# goal must never be unreachable because of a decision taken in a file this test
# cannot see. Nothing here imports product code — it only teaches the import
# system where `packages.store_agent` and `apps.seller_reference` live, so a
# missing tree still raises a clean ModuleNotFoundError inside the test body.
# ---------------------------------------------------------------------------

_HYPHEN_ALIASES = (
    ("packages.store_agent", ("packages", "store-agent")),
    ("apps.seller_reference", ("apps", "seller-reference")),
)


def _repo_root() -> pathlib.Path:
    # <repo>/.swarm-loop/acceptance/test_e4_store_agent.py -> <repo>
    return pathlib.Path(__file__).resolve().parents[2]


def _register_namespace(dotted: str, directory: pathlib.Path) -> None:
    """Idempotently register `dotted` as a namespace package rooted at `directory`."""
    parts = dotted.split(".")
    for index, part in enumerate(parts):
        name = ".".join(parts[: index + 1])
        if index == len(parts) - 1:
            target = directory
        else:
            target = _repo_root().joinpath(*parts[: index + 1])
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
            spec.submodule_search_locations = [str(target)]
            module.__spec__ = spec
            module.__path__ = spec.submodule_search_locations
            module.__package__ = name
            sys.modules[name] = module
            if index:
                setattr(sys.modules[".".join(parts[:index])], part, module)
        else:
            search = getattr(module, "__path__", None)
            if search is not None and str(target) not in list(search):
                search.append(str(target))


def _bootstrap_imports() -> None:
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    for dotted, rel in _HYPHEN_ALIASES:
        _register_namespace(dotted, _repo_root().joinpath(*rel))


# ---------------------------------------------------------------------------
# Plain-data helpers. Product objects may be pydantic models, dataclasses or
# dicts; every assertion below runs against a normalized plain-Python view so a
# reasonable redesign of the model layer cannot break a frozen goal.
# ---------------------------------------------------------------------------


def _plain(obj, _depth: int = 0):
    if _depth > 40:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_plain(v, _depth + 1) for v in obj]
    if isinstance(obj, (set, frozenset)):
        items = [_plain(v, _depth + 1) for v in obj]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if isinstance(obj, dict):
        return {str(k): _plain(v, _depth + 1) for k, v in obj.items()}
    for attr in ("model_dump", "_asdict", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _plain(fn(), _depth + 1)
            except Exception:  # pragma: no cover - fall through to the next shape
                pass
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields:
        return {str(f): _plain(getattr(obj, f, None), _depth + 1) for f in fields}
    namespace = getattr(obj, "__dict__", None)
    if isinstance(namespace, dict) and namespace:
        return {
            str(k): _plain(v, _depth + 1)
            for k, v in namespace.items()
            if not str(k).startswith("_")
        }
    return str(obj)


def _canon(obj) -> str:
    """A stable canonical string for any product object — the equality workhorse."""
    return json.dumps(_plain(obj), sort_keys=True, default=str)


def _walk(plain):
    """Yield every dict and list node in a normalized plain structure."""
    stack = [plain]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _claims_in(obj) -> list:
    """Every claim-shaped node reachable from `obj` (a dict carrying `provenance`)."""
    return [n for n in _walk(_plain(obj)) if isinstance(n.get("provenance"), (dict, str))]


def _find_values(obj, key: str) -> list:
    return [n[key] for n in _walk(_plain(obj)) if key in n]


def _first_value(obj, key: str, default=None):
    found = _find_values(obj, key)
    return found[0] if found else default


def _source_of(claim) -> str:
    prov = _plain(claim).get("provenance") if isinstance(_plain(claim), dict) else None
    if isinstance(prov, dict):
        return str(prov.get("source", ""))
    return str(prov or "")


def _claim_identity(claim) -> tuple:
    node = _plain(claim)
    prov = node.get("provenance") if isinstance(node, dict) else {}
    prov = prov if isinstance(prov, dict) else {}
    return (
        str(node.get("key")),
        json.dumps(node.get("value"), sort_keys=True, default=str),
        str(prov.get("source")),
        str(prov.get("ref")),
    )


def _norm(value) -> str:
    return str(value).strip().lower()


class _Recorder:
    """An in-process fake for an injected sink / submitter / queue.

    Callable, and aliased under the handful of plausible method names, so the
    frozen goal turns on *whether* the product submitted or logged — never on the
    spelling of the method it used to do so.
    """

    def __init__(self, name: str = "recorder") -> None:
        self.name = name
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": _plain(list(args)), "kwargs": _plain(kwargs)})
        return None

    write = append = submit = send = log = record = put = enqueue = __call__

    @property
    def count(self) -> int:
        return len(self.calls)

    def payloads(self) -> list:
        out = []
        for call in self.calls:
            out.extend(call["args"])
            out.extend(call["kwargs"].values())
        return out


# ---------------------------------------------------------------------------
# Fixture data. Plain dicts shaped by DESIGN.md §Interfaces (Envelope, Claim,
# Provenance, Intent, BuyerProfile, BidRequest, TrustEventPayload). Nothing here
# is read from disk and nothing here is random.
# ---------------------------------------------------------------------------

CLUSTER = "cluster-warm-layers"
STORE_ID = "store-alpha"
LIST_PRICE = 100.0


def _prov(source: str, ref: str) -> dict:
    return {
        "source": source,
        "ref": ref,
        "observed_at": "2026-01-01T00:00:00Z",
        "authority_rank": 1,
    }


def _envelope() -> dict:
    """An approved envelope: max discount 20%, one product walled by a price floor."""
    return {
        "store_id": STORE_ID,
        "version": 3,
        "floors": [
            {"product_ref": "prod-floor", "min_price": 95.0},
            {"product_ref": "prod-cap", "min_price": 10.0},
        ],
        "max_discount_pct": 20.0,
        "budget_cap": 500.0,
        "pursue_clusters": [CLUSTER],
        "standing_commitments": [
            {
                "key": "free_returns",
                "value": "30 days",
                "provenance": _prov("owner_statement", "envelope:store-alpha:v3#free_returns"),
            },
            {
                "key": "ships_within",
                "value": "2 business days",
                "provenance": _prov("owner_statement", "envelope:store-alpha:v3#ships_within"),
            },
        ],
        "activation": "shadow",
    }


def _store_context(**overrides) -> dict:
    ctx = {
        "store_id": STORE_ID,
        "envelope": _envelope(),
        "catalog": {
            "prod-cap": {
                "product_ref": "prod-cap",
                "list_price": LIST_PRICE,
                "material": "merino wool",
                "gtin": "00000000000017",
            },
            "prod-floor": {
                "product_ref": "prod-floor",
                "list_price": LIST_PRICE,
                "material": "alpaca",
                "gtin": "00000000000024",
            },
        },
        "live_state": {
            "prod-cap": {"in_stock": True, "units_left": 7},
            "prod-floor": {"in_stock": True, "units_left": 3},
        },
        "learned_policy": None,
        "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }
    ctx.update(overrides)
    return ctx


def _intent() -> dict:
    return {
        "intent_id": "int-0001",
        "cluster_id": CLUSTER,
        "query": "a warm mid-layer for cold commutes",
        "category": "outerwear",
        "hard_constraints": [{"field": "material", "op": "eq", "value": "merino wool"}],
        "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
        "ship_to": "US-CA",
        "currency": "USD",
        "budget_band": "50-150",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1",
    }


def _profile() -> dict:
    return {
        "pseudonym": "pseu-0001",
        "buckets": {
            "budget_band": "50-150",
            "category_affinity": ["outerwear"],
            "frequency_tier": "occasional",
            "region": "US-W",
            "first_time": True,
        },
    }


def _bid_request(auction_id: str = "auc-0001") -> dict:
    return {
        "auction_id": auction_id,
        "intent": _intent(),
        "profile": _profile(),
        "respond_by": "2999-01-01T00:00:00Z",
    }


def _trust_event_payload() -> dict:
    return {
        "store_id": STORE_ID,
        "event": {
            "event_id": "evt-0001",
            "ts": "2026-01-02T00:00:00Z",
            "kind": "feedback",
            "auction_id": "auc-0000",
            "store_id": STORE_ID,
            "order_ref": "ord-0001",
            "payload": {"matched_pitch": False, "reason": "ships_within missed"},
        },
        "dim": "shipped_on_time",
        "delta": -0.35,
        "pseudonymous_context": {"cluster_id": CLUSTER, "pseudonym": "pseu-0002"},
    }


def _build_hooks(tool_hooks_cls, context: dict):
    """Construct the hooks object from a store context (mapping or kwargs form)."""
    try:
        return tool_hooks_cls(context)
    except TypeError:
        return tool_hooks_cls(**context)


def _authorize(hooks, denied, product_ref: str, pct: float):
    """Return (was_denied, result) for an authorize_discount call.

    `Denied` may be a sentinel, a marker class or an exception class; only a
    Denied-shaped exception is swallowed, so a wrong call signature still fails
    the test loudly instead of masquerading as a denial.
    """
    catchable = denied if isinstance(denied, type) and issubclass(denied, BaseException) else ()
    try:
        result = hooks.authorize_discount(product_ref, pct)
    except catchable:
        return True, None
    if isinstance(denied, type):
        if isinstance(result, denied):
            return True, result
    elif result is denied or result == denied:
        return True, result
    if result is None:
        return True, result
    return False, result


def _outcome_records(win_depth: float, loss_depth: float, n: int = 40) -> list:
    """Own-store outcomes: `win_depth` converts, `loss_depth` does not."""
    records = []
    for i in range(n):
        records.append(
            {
                "cluster_id": CLUSTER,
                "store_id": STORE_ID,
                "discount_depth": win_depth,
                "commitments": ["free_returns"],
                "won": True,
                "order_ref": f"ord-win-{i:03d}",
            }
        )
        records.append(
            {
                "cluster_id": CLUSTER,
                "store_id": STORE_ID,
                "discount_depth": loss_depth,
                "commitments": ["free_returns"],
                "won": False,
                "order_ref": f"ord-loss-{i:03d}",
            }
        )
    return records


def _strip_discount_fields(node):
    """Recursively drop every key whose name mentions a discount."""
    if isinstance(node, dict):
        return {
            k: _strip_discount_fields(v)
            for k, v in node.items()
            if "discount" not in str(k).lower()
        }
    if isinstance(node, list):
        return [_strip_discount_fields(v) for v in node]
    return node


def _prior_records() -> list:
    """Pitch / value-prop outcomes that also carry discount fields (which must be ignored)."""
    records = []
    for i in range(24):
        won = i % 3 != 0
        records.append(
            {
                "cluster_id": CLUSTER,
                "store_id": f"store-{i % 4}",
                "value_prop": "durability" if won else "price",
                "pitch_claims": ["free_returns", "ships_within"],
                "commitments": ["free_returns"],
                "won": won,
                "discount_depth": 0.2 if won else 0.05,
                "discount_pct": 20.0 if won else 5.0,
                "offer": {
                    "unit_price": LIST_PRICE,
                    "discount": {"type": "percentage", "value": 20.0 if won else 5.0},
                },
            }
        )
    return records


def _mean(values) -> float:
    values = list(values)
    return sum(values) / float(len(values))


def _load_fixture_manifest():
    """Load the human-approved fixture manifest (T-080's artifact) from `fixtures/`."""
    fixtures = _repo_root() / "fixtures"
    if not fixtures.is_dir():
        pytest.fail(f"the human-approved fixture manifest directory {fixtures} does not exist")
    candidates = sorted(fixtures.rglob("manifest*.json")) + sorted(fixtures.rglob("*manifest.json"))
    seen, manifests = set(), []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            manifests.append((path, json.loads(path.read_text())))
        except Exception as exc:  # noqa: BLE001 - a malformed manifest is a real failure
            pytest.fail(f"fixture manifest {path} does not parse as JSON: {exc}")
    if not manifests:
        pytest.fail(f"no manifest*.json found under {fixtures}")
    for path, data in manifests:
        if _find_values(data, "personas"):
            return path, data
    pytest.fail(
        "no fixture manifest under fixtures/ declares a `personas` block: "
        + ", ".join(str(p) for p, _ in manifests)
    )


# ---------------------------------------------------------------------------
# T-040 — tool hooks are the only way facts and discounts enter a bid
# ---------------------------------------------------------------------------

HOOK_SOURCE_CLASSES = {
    "get_product_fact": "scraped",
    "get_live_state": "pixel_feed",
    "get_owner_commitments": "owner_statement",
    "authorize_discount": "envelope_rule",
    "choose_policy_action": "learned_policy",
    "get_network_prior": "network",
}


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-040")
def test_every_tool_hook_returns_a_provenance_tagged_claim():
    """R8: each of the six tool hooks stamps the provenance source class DESIGN assigns it."""
    _bootstrap_imports()
    from packages.store_agent.src.hooks import ToolHooks

    hooks = _build_hooks(ToolHooks, _store_context())

    missing = [name for name in HOOK_SOURCE_CLASSES if not callable(getattr(hooks, name, None))]
    assert not missing, f"tool hooks missing from the store-agent contract: {missing}"

    calls = {
        "get_product_fact": lambda: hooks.get_product_fact("prod-cap", "material"),
        "get_live_state": lambda: hooks.get_live_state("prod-cap"),
        "get_owner_commitments": lambda: hooks.get_owner_commitments(CLUSTER),
        "authorize_discount": lambda: hooks.authorize_discount("prod-cap", 5.0),
        "choose_policy_action": lambda: hooks.choose_policy_action(
            {"cluster_id": CLUSTER, "intent": _intent(), "product_ref": "prod-cap"}
        ),
        "get_network_prior": lambda: hooks.get_network_prior(CLUSTER),
    }

    for hook_name, expected_source in HOOK_SOURCE_CLASSES.items():
        returned = calls[hook_name]()
        claims = _claims_in(returned)
        assert claims, f"{hook_name} returned no provenance-tagged value: {returned!r}"
        sources = {_source_of(c) for c in claims}
        assert sources == {expected_source}, (
            f"{hook_name} must stamp provenance source {expected_source!r}; got {sorted(sources)}"
        )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-040")
def test_authorize_discount_denies_below_floor_and_over_cap():
    """R8: the envelope's price floor and max-discount cap are independent hard walls."""
    _bootstrap_imports()
    from packages.store_agent.src.hooks import Denied, ToolHooks

    hooks = _build_hooks(ToolHooks, _store_context())

    # prod-cap: floor is far below list price, so only the 20% cap can bite.
    denied_over_cap, _ = _authorize(hooks, Denied, "prod-cap", 25.0)
    assert denied_over_cap, "a 25% request must be denied against a 20% max_discount_pct"

    denied_at_cap, at_cap_claim = _authorize(hooks, Denied, "prod-cap", 20.0)
    assert not denied_at_cap, "a request exactly at max_discount_pct must be authorized"

    # prod-floor: 10% is inside the 20% cap but takes the price to 90 < the 95 floor.
    denied_below_floor, _ = _authorize(hooks, Denied, "prod-floor", 10.0)
    assert denied_below_floor, (
        "a request inside the discount cap that breaches the product price floor must be denied"
    )

    denied_inside_walls, granted = _authorize(hooks, Denied, "prod-floor", 3.0)
    assert not denied_inside_walls, "a request inside both walls must be authorized"

    for claim in (at_cap_claim, granted):
        claims = _claims_in(claim)
        assert claims, f"an authorized discount must return a provenance-tagged claim: {claim!r}"
        assert {_source_of(c) for c in claims} == {"envelope_rule"}, (
            f"an authorized discount must carry provenance source 'envelope_rule': {claim!r}"
        )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-040")
def test_a_claim_built_outside_the_hooks_is_refused_at_the_boundary():
    """R8/S5: a claim assembled agent-side is refused even when its provenance looks legitimate."""
    _bootstrap_imports()
    from packages.store_agent.src.hooks import (
        HookProvenanceError,
        ToolHooks,
        enforce_hook_provenance,
    )

    hooks = _build_hooks(ToolHooks, _store_context())
    genuine = _claims_in(hooks.get_owner_commitments(CLUSTER))
    assert genuine, "get_owner_commitments must emit at least one claim for the guard to admit"

    # Hook-emitted claims pass the guard.
    enforce_hook_provenance(genuine, hooks)

    # A smuggled claim wearing a legitimate-looking provenance tag must NOT pass:
    # a guard that only inspects `provenance.source` would wave this through.
    smuggled = {
        "key": "free_returns",
        "value": "90 days",
        "provenance": _prov("owner_statement", "envelope:store-alpha:v3#free_returns"),
    }
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance(list(genuine) + [smuggled], hooks)


# ---------------------------------------------------------------------------
# T-041 — advocate runtime: deterministic cold bid, hook-only claims
# ---------------------------------------------------------------------------


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-041")
def test_cold_agent_emits_the_deterministic_default_bid():
    """R10/cold start: with no learned policy the bid is list price + standing commitments, twice identically."""
    _bootstrap_imports()
    from packages.store_agent.src.runtime import bid as make_bid

    context = _store_context(learned_policy=None)
    request = _bid_request()

    first = make_bid(request, context)
    second = make_bid(_bid_request(), _store_context(learned_policy=None))

    assert _canon(first) == _canon(second), (
        "the cold-start bid must be byte-identical across two calls with identical inputs"
    )

    plain = _plain(first)
    offer = _first_value(plain, "offer")
    assert isinstance(offer, dict), f"the cold bid must carry an Offer: {plain!r}"

    product_ref = offer.get("product_ref")
    catalog = context["catalog"]
    assert product_ref in catalog, f"the cold bid must offer a catalog product, got {product_ref!r}"

    unit_price = float(offer["unit_price"])
    assert unit_price == float(catalog[product_ref]["list_price"]), (
        "the cold-start unit price must be the list price — no LLM improvisation of price"
    )
    total = offer.get("total_price")
    assert total is None or float(total) == unit_price, (
        "with no envelope intro rule the cold-start total must equal the list price"
    )
    discount = offer.get("discount")
    assert not discount or float(_first_value(discount, "value", 0) or 0) == 0.0, (
        f"the cold-start bid must carry no discount when the envelope defines no intro rule: {discount!r}"
    )

    commitment_keys = {str(c.get("key")) for c in _plain(offer.get("commitments") or [])}
    envelope_keys = {str(c["key"]) for c in context["envelope"]["standing_commitments"]}
    assert commitment_keys == envelope_keys, (
        f"cold-start commitments must be exactly the envelope's standing commitments: "
        f"{sorted(commitment_keys)} != {sorted(envelope_keys)}"
    )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-041")
def test_every_claim_in_a_hosted_bid_traces_to_a_hook_call():
    """S5/R8: the set difference between a hosted bid's claims and the hooks' emissions is empty."""
    _bootstrap_imports()
    from packages.store_agent.src.hooks import ToolHooks
    from packages.store_agent.src.runtime import bid as make_bid

    context = _store_context()
    hooks = _build_hooks(ToolHooks, context)
    emitted = make_bid(_bid_request(), context, hooks=hooks)

    bid_claims = _claims_in(emitted)
    assert bid_claims, "a hosted bid must carry at least one claim for this criterion to mean anything"
    assert getattr(hooks, "call_log", None), "the hooks facade must record the calls the runtime made"

    hook_claims = _claims_in(getattr(hooks, "emitted_claims", []))
    assert hook_claims, "the hooks facade must expose the claims it emitted"

    smuggled = {_claim_identity(c) for c in bid_claims} - {_claim_identity(c) for c in hook_claims}
    assert not smuggled, f"these bid claims did not come from a tool hook: {sorted(smuggled)}"


# ---------------------------------------------------------------------------
# T-042 — the store loop learns from its own outcomes only
# ---------------------------------------------------------------------------


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-042")
def test_store_discount_depth_shifts_toward_its_own_winners():
    """R17/S4: seeded outcomes move the store's own sampled discount-depth distribution toward its winners."""
    _bootstrap_imports()
    from packages.store_agent.src.learning import (
        build_network_prior,
        initial_state,
        sample_depth,
        update,
    )

    prior = build_network_prior(_prior_records())
    base = initial_state(prior)

    def depth_mean(state) -> float:
        return _mean(sample_depth(state, CLUSTER, seed) for seed in range(400))

    # sample_depth is seeded, so it is a pure function of (state, cluster, seed).
    assert sample_depth(base, CLUSTER, 7) == sample_depth(base, CLUSTER, 7), (
        "sample_depth must be deterministic for a fixed seed"
    )

    base_mean = depth_mean(base)
    deep_mean = depth_mean(update(initial_state(prior), _outcome_records(0.20, 0.00)))
    shallow_mean = depth_mean(update(initial_state(prior), _outcome_records(0.00, 0.20)))

    assert deep_mean > base_mean, (
        f"outcomes favouring deep discounts must raise sampled depth: {deep_mean} !> {base_mean}"
    )
    assert shallow_mean < base_mean, (
        f"outcomes favouring shallow discounts must lower sampled depth: {shallow_mean} !< {base_mean}"
    )
    assert deep_mean > shallow_mean


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-042")
def test_network_prior_builder_never_reads_discount_fields():
    """R17: discount elasticity is never pooled across stores — the prior ignores every discount field."""
    _bootstrap_imports()
    from packages.store_agent.src.learning import build_network_prior

    with_discounts = _prior_records()
    without_discounts = _strip_discount_fields(with_discounts)
    assert _canon(with_discounts) != _canon(without_discounts), (
        "the fixture must actually carry discount fields for this test to discriminate"
    )

    prior_with = build_network_prior(with_discounts)
    prior_without = build_network_prior(without_discounts)
    assert _canon(prior_with) == _canon(prior_without), (
        "the network prior must be byte-identical with and without discount fields"
    )

    # ...and it must not be a constant: a builder that always returns {} would pass
    # the equality above while measuring nothing.
    assert _canon(prior_with) not in ("null", "{}", "[]", '""'), (
        f"the network prior must carry content, got {_canon(prior_with)}"
    )
    flipped = [dict(r, won=not r["won"]) for r in with_discounts]
    assert _canon(build_network_prior(flipped)) != _canon(prior_with), (
        "the network prior must respond to pitch/value-prop outcomes"
    )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-042")
def test_two_stores_learn_independently_from_identical_priors():
    """R17: identical priors + different own-outcomes yield different states, and neither leaks into the other."""
    _bootstrap_imports()
    from packages.store_agent.src.learning import build_network_prior, initial_state, update

    prior = build_network_prior(_prior_records())
    state_a = initial_state(prior)
    state_b = initial_state(prior)

    a_before = _canon(state_a)
    b_before = _canon(state_b)
    assert a_before == b_before, "two stores seeded from one prior must start identical"

    updated_a = update(state_a, _outcome_records(0.20, 0.00))
    assert _canon(state_b) == b_before, "updating store A must not mutate store B's state"

    updated_b = update(state_b, _outcome_records(0.00, 0.20))
    assert _canon(updated_a) != a_before, "an update fed real outcomes must change the state"
    assert _canon(updated_a) != _canon(updated_b), (
        "two stores fed different outcomes must end in different learned states"
    )


# ---------------------------------------------------------------------------
# T-043 — shadow mode, activation, and trust-event intake
# ---------------------------------------------------------------------------


def _make_runner(agent_runner_cls, mode: str = "shadow"):
    sink = _Recorder("sink")
    submitter = _Recorder("submitter")
    runner = agent_runner_cls(_store_context(), sink=sink, submitter=submitter, mode=mode)
    return runner, sink, submitter


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-043")
def test_shadow_mode_logs_bids_and_submits_nothing():
    """R7: an un-activated store computes and logs full bids with rationale, and submits nothing."""
    _bootstrap_imports()
    from packages.store_agent.src.modes import AgentRunner

    runner, sink, submitter = _make_runner(AgentRunner, mode="shadow")
    for i in range(3):
        runner.run(_bid_request(f"auc-{i:04d}"))

    assert submitter.count == 0, (
        f"a shadow-mode store must submit nothing; it submitted {submitter.count} time(s)"
    )
    assert sink.count == 3, f"a shadow-mode store must log every would-be bid; logged {sink.count}"

    for payload in sink.payloads():
        offer = _first_value(payload, "offer")
        assert isinstance(offer, dict) and offer.get("unit_price") is not None, (
            f"a shadow log entry must contain a fully-formed offer: {payload!r}"
        )
        rationale = _first_value(payload, "rationale")
        assert isinstance(rationale, str) and rationale.strip(), (
            f"a shadow log entry must carry a non-empty rationale: {payload!r}"
        )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-043")
def test_activation_flips_to_submitting_without_restart():
    """R7/R6: activation starts submission on the same object, and the kill switch stops it again."""
    _bootstrap_imports()
    from packages.store_agent.src.modes import AgentRunner

    runner, sink, submitter = _make_runner(AgentRunner, mode="shadow")

    runner.run(_bid_request("auc-0001"))
    assert submitter.count == 0

    runner.mode = "active"
    runner.run(_bid_request("auc-0002"))
    assert submitter.count == 1, (
        "flipping the same runner to active must submit the next bid without a restart"
    )

    runner.mode = "killed"
    runner.run(_bid_request("auc-0003"))
    assert submitter.count == 1, "the kill switch must stop submission again on the same object"

    assert sink.count >= 3, "every mode must still log the bid it computed"


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-043")
def test_an_injected_trust_event_changes_the_next_bid_rationale():
    """R13: a pushed TrustEventPayload deterministically changes the next bid's rationale."""
    _bootstrap_imports()
    from packages.store_agent.src.modes import AgentRunner

    def rationale_after(events) -> str:
        runner, sink, _submitter = _make_runner(AgentRunner, mode="shadow")
        for event in events:
            runner.ingest_trust_event(event)
        runner.run(_bid_request("auc-0007"))
        rationale = _first_value(sink.payloads(), "rationale")
        assert isinstance(rationale, str) and rationale.strip(), (
            f"the logged bid must carry a non-empty rationale, got {rationale!r}"
        )
        return rationale

    baseline = rationale_after([])
    after_event = rationale_after([_trust_event_payload()])
    after_event_again = rationale_after([_trust_event_payload()])

    assert after_event != baseline, (
        "an injected trust event must change the next bid's rationale/commitment posture"
    )
    assert after_event == after_event_again, (
        "the same trust event twice must produce the same change — deterministic under doubles"
    )


# ---------------------------------------------------------------------------
# T-044 — the signed external door
# ---------------------------------------------------------------------------

EXTERNAL_KEY = "external-secret-key-0001"
EXTERNAL_STORE = "store-external-1"


def _external_payload(store_id: str = EXTERNAL_STORE, expires_at: str = "2999-01-01T00:00:00Z") -> dict:
    return {
        "auction_id": "auc-0100",
        "store_id": store_id,
        "offer": {
            "product_ref": "ext-prod-1",
            "unit_price": 89.0,
            "total_price": 89.0,
            "discount": None,
            "commitments": [],
            "expires_at": expires_at,
        },
        "claims": [
            {
                "key": "material",
                "value": "merino wool",
                "provenance": _prov("seller_asserted", "pitch:ext-1#material"),
            }
        ],
        "message": "Warmest merino mid-layer on the market, guaranteed.",
        "agent_version": "ext-0.1.0",
        "schema_version": "1",
    }


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_signed_external_bid_is_accepted_and_enqueued_for_verification():
    """R8/R18: a bid signed with a registered key is admitted as unverified and queued for verification."""
    _bootstrap_imports()
    from packages.store_agent.src.external import receive_bid, sign_bid

    payload = _external_payload()
    keyring = {EXTERNAL_STORE: EXTERNAL_KEY}
    queue = _Recorder("verification_queue")

    result = receive_bid(payload, sign_bid(payload, EXTERNAL_KEY), keyring, queue=queue)
    plain = _plain(result)

    assert _first_value(plain, "accepted") is True, (
        f"a correctly signed external bid must be accepted, got {plain!r}"
    )
    marked_unverified = _first_value(plain, "verified") is False or _norm(
        _first_value(plain, "verification_status", _first_value(plain, "status", ""))
    ) in {"unverified", "pending", "pending_verification"}
    assert marked_unverified, (
        f"an admitted external bid must be marked unverified, never silently trusted: {plain!r}"
    )
    assert queue.count == 1, (
        f"an accepted external bid must be enqueued for claim extraction + verification exactly "
        f"once; queue saw {queue.count} call(s)"
    )
    queued_claims = _claims_in(queue.payloads())
    assert any(_source_of(c) == "seller_asserted" for c in queued_claims), (
        f"the queued work item must carry the seller_asserted claims to verify: "
        f"{_plain(queue.payloads())!r}"
    )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_external_bid_with_a_bad_signature_is_rejected():
    """R8/C10: wrong, absent and replayed signatures — and expired or blacklisted bids — reject before enqueue."""
    _bootstrap_imports()
    from packages.store_agent.src.external import receive_bid, sign_bid

    keyring = {EXTERNAL_STORE: EXTERNAL_KEY}
    good_payload = _external_payload()
    good_signature = sign_bid(good_payload, EXTERNAL_KEY)

    # Arm the test: the happy path must work, so a rejection below is a real
    # rejection rather than a TypeError from a mismatched call signature.
    control_queue = _Recorder("control")
    control = _plain(receive_bid(good_payload, good_signature, keyring, queue=control_queue))
    assert _first_value(control, "accepted") is True, f"control bid must be accepted: {control!r}"
    assert control_queue.count == 1

    def rejects(payload, signature, **kwargs) -> bool:
        queue = _Recorder("queue")
        try:
            result = receive_bid(payload, signature, keyring, queue=queue, **kwargs)
        except Exception:  # noqa: BLE001 - raising is a valid way to refuse
            return queue.count == 0
        return _first_value(_plain(result), "accepted") is not True and queue.count == 0

    assert rejects(good_payload, sign_bid(good_payload, "wrong-key")), "wrong key must reject"
    assert rejects(good_payload, "not-a-signature"), "a garbage signature must reject"
    assert rejects(good_payload, None), "an absent signature must reject"
    assert rejects(good_payload, sign_bid(_external_payload("store-external-2"), EXTERNAL_KEY)), (
        "a signature replayed from a different payload must reject"
    )

    expired = _external_payload(expires_at="2000-01-01T00:00:00Z")
    assert rejects(expired, sign_bid(expired, EXTERNAL_KEY)), (
        "an expired external offer must reject before enqueue"
    )
    assert rejects(good_payload, good_signature, blacklist=[EXTERNAL_STORE]), (
        "a blacklisted external store must reject before enqueue"
    )


# ---------------------------------------------------------------------------
# T-045 — the adversarial reference persona
# ---------------------------------------------------------------------------


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-045")
def test_aggressive_persona_emits_exactly_the_manifest_scripted_claims():
    """R18/S8/A3: the aggressive persona's pitch is exactly the manifest's scripted claims, all seller_asserted."""
    _bootstrap_imports()
    from apps.seller_reference.src.personas import build_persona

    manifest_path, manifest = _load_fixture_manifest()
    personas = _first_value(manifest, "personas", {})
    assert isinstance(personas, dict) and "aggressive" in personas, (
        f"{manifest_path} must script the aggressive persona; got {sorted(_plain(personas))}"
    )
    aggressive = personas["aggressive"]
    scripted = aggressive.get("scripted_claims") or aggressive.get("claims")
    assert isinstance(scripted, list) and scripted, (
        f"{manifest_path} must script the aggressive persona's claims (ground truth for S8)"
    )

    intent = (
        _first_value(manifest, "fixture_intent")
        or _first_value(manifest, "intent")
        or (_first_value(manifest, "intents") or [None])[0]
    )
    assert isinstance(intent, dict), f"{manifest_path} must define the fixture intent the persona answers"

    pitch = build_persona("aggressive").pitch(intent)
    emitted = _claims_in(pitch)
    assert emitted, f"the aggressive persona emitted no claims for the manifest intent: {_plain(pitch)!r}"

    expected_keys = {_norm(c["key"]) for c in scripted}
    emitted_keys = {_norm(c.get("key")) for c in emitted}
    assert emitted_keys == expected_keys, (
        "the aggressive persona must emit exactly the manifest-scripted claims — no more, no fewer: "
        f"extra={sorted(emitted_keys - expected_keys)} missing={sorted(expected_keys - emitted_keys)}"
    )

    by_key = {_norm(c.get("key")): c for c in emitted}
    for scripted_claim in scripted:
        if "value" in scripted_claim:
            key = _norm(scripted_claim["key"])
            assert _norm(by_key[key].get("value")) == _norm(scripted_claim["value"]), (
                f"claim {key!r} must carry the manifest's scripted value "
                f"{scripted_claim['value']!r}, got {by_key[key].get('value')!r}"
            )

    bad_sources = {_source_of(c) for c in emitted} - {"seller_asserted"}
    assert not bad_sources, (
        f"every persona claim must arrive with provenance source 'seller_asserted'; got {sorted(bad_sources)}"
    )
