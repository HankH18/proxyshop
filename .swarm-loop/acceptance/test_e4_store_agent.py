"""Epic E4 — Store agent & sellers. FROZEN acceptance criteria.

Covers the promised behaviour of the hosted store advocate and the unharnessed
reference personas:

* R7  — shadow mode computes and logs bids but submits nothing; activation flips
        submission on without a restart, and the kill switch flips it off again.
* R8  — every claim and every discount a hosted agent emits arrives through a
        provenance-tagged tool hook; envelope walls (floor / max discount) are hard;
        a claim assembled outside the hooks is refused at the boundary; external
        bids enter signed and are admitted as *unverified* rather than trusted.
* C10 — the external door's signing envelope is REQUIRED, not decorative: `signer_id`,
        `key_id`, `issued_at` and a `nonce` idempotency key ride on every submission,
        are covered by deterministic canonical signing bytes, are checked for freshness,
        select a key out of a rotating per-signer keyring, and are remembered against
        replay until after the auction deadline. (Harness amendment 1: these four fields
        were optional only because the original frozen fixture happened to omit them.)
* R10 — the cold agent (no learned policy) emits a deterministic default bid at
        list price with the envelope's standing commitments and no improvisation.
* R13 — an injected TrustEventPayload deterministically changes the next bid's
        posture/rationale.
* R17 — the store loop learns from its OWN outcomes only; the network prior builder
        never reads discount fields; two stores learn independently.
* R18 / S8 / A3 — the aggressive reference persona emits exactly the claims the
        human-approved fixture manifest scripts, all tagged `seller_asserted`.
* S4  — the store-side half of the seeded-determinism criterion.
* S5  — the hosted-trace half: every claim in an emitted bid traces to a hook call,
        `offer.discount` included. (Harness amendment 6, ESC-007: a `Discount` carries
        provenance but no `key`, so identifying it by `key` collapsed every discount onto
        one identity no hook claim can equal and read a legitimate hook-granted discount
        as smuggled. It is identified by the grant it cites instead.)

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


#: Identity slot for a provenance-bearing node that carries no `key` of its own.
#: Chosen so it can never collide with a real claim key: `contracts` claim keys are
#: snake_case identifiers, and this is not one.
UNKEYED = "<unkeyed>"

#: The ONLY hook claim an unkeyed node may cite. `contracts.Discount` is the one
#: provenance-bearing model in the protocol with no `key` (`Claim.key` is required,
#: `min_length=1`), and `runtime.bidding._discount` builds it by copying the
#: `authorized_discount_pct` grant's own provenance onto the granted depth. So the
#: unkeyed identity is lent to that grant and to nothing else — matching production's
#: own `hooks.provenance.PRODUCT_SCOPED_CLAIM_KEYS`, which is the same single key.
DISCOUNT_GRANT_KEY = "authorized_discount_pct"


def _claim_identity(claim) -> tuple:
    """A ledger identity for one provenance-bearing node: (key, value, source, ref).

    Harness amendment 6 (ESC-007). A `Claim` has a `key` and identifies by it. `offer.discount`
    does NOT — `contracts.Discount` is `(type, value, provenance)` — and since T-135 the bid
    boundary REQUIRES it to carry a provenance block, because otherwise an attacker drops the
    block instead of relabelling it and the discount walks through. So the discount is a
    provenance-bearing node with no key, and reading `node.get("key")` off it produced the
    string `"None"` for every discount ever emitted: every discount collapsed onto one identity
    that no hook claim can ever equal, and a legitimate hook-granted discount read as smuggled.
    Measured before this amendment, on a bid discounted by the envelope's intro rule:
    ``smuggled == [('None', '15.0', 'envelope_rule',
    'envelope:store-alpha:v3#max_discount_pct@prod-cap')]``.

    So an unkeyed node is identified by the provenance it cites instead of by a key it does not
    have. This LOOSENS NOTHING. A discount is admissible because a real `authorized_discount_pct`
    grant was issued beside it, and it cites that grant by carrying the grant's provenance,
    copied — same `source`, same product-scoped `ref` — at the depth the grant actually
    authorized. `value`, `source` and `ref` must ALL still match something a hook really emitted
    (see :func:`_hook_identities`), so a discount deeper than its grant, or citing a grant that
    was never issued, still fails to trace. It only lets the helper SEE a node it previously
    could not match at all.
    """
    node = _plain(claim)
    node = node if isinstance(node, dict) else {}
    prov = node.get("provenance")
    prov = prov if isinstance(prov, dict) else {}
    return (
        str(node["key"]) if "key" in node else UNKEYED,
        json.dumps(node.get("value"), sort_keys=True, default=str),
        str(prov.get("source")),
        str(prov.get("ref")),
    )


def _hook_identities(claim) -> set:
    """Every identity a bid node may legitimately present as having come from `claim`.

    For almost every hook claim: exactly ONE — the claim's own keyed identity. A `list_price`,
    a `units_left`, a `material`, a `policy_action` authorizes nothing keyless; it is quoted in
    a bid as a `Claim`, under its own key, and it identifies by that key.

    The single exception is the `authorized_discount_pct` grant, which gets a SECOND identity:
    the one an UNKEYED node citing it would carry. That is what `offer.discount` presents —
    `runtime.bidding._discount` copies the grant's own `provenance` onto the granted depth, so
    the discount repeats the grant's `value`, `source` and `ref` and has no key of its own.

    **Why the restriction is the whole point.** Lending the unkeyed slot to every hook claim
    weakens this criterion instead of preserving it: a forged discount could then copy ANY real
    emission's (value, source, ref) and trace. Measured against this file's own discounted
    fixture, with the widening unrestricted, all three of these read as fully traced —

    * ``value=100.0`` carrying the scraped list price's provenance verbatim (`scraped`,
      ``catalog:store-alpha:prod-cap#list_price``) — a 100%-off discount no `authorize_discount`
      call ever granted;
    * ``value=7`` carrying `pixel_feed` / ``pixel:store-alpha:prod-cap#units_left``;
    * ``value=100.0`` carrying the `prod-floor` list-price ref.

    None of the three can be produced by `authorize_discount`, and all three are caught again
    once the widening is confined to the grant.

    So: a keyed bid node can never match the unkeyed form (a node that has a key always
    identifies by it), and an unkeyed node can only ever match the ONE grant claim, at the value
    AND source AND ref that grant actually carried. A discount deeper than its grant, citing a
    ref that was never minted, wearing a relabelled source, or piggybacking on a non-grant
    emission, all still fail to trace.
    """
    keyed = _claim_identity(claim)
    if keyed[0] != DISCOUNT_GRANT_KEY:
        return {keyed}
    return {keyed, (UNKEYED,) + keyed[1:]}


def _hook_granted_identities(hook_claims) -> set:
    """The union of :func:`_hook_identities` over everything the hooks emitted."""
    granted: set = set()
    for claim in hook_claims:
        granted |= _hook_identities(claim)
    return granted


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

#: The envelope's cold-start intro discount rule (DESIGN.md R10: "list price, envelope standing
#: commitments, intro discount rule if the envelope defines one"). Inside the 20% cap and far
#: above the `prod-cap` floor, so the envelope grants it and the bid really is discounted.
INTRO_DISCOUNT_KEY = "intro_discount_pct"
INTRO_DISCOUNT_PCT = 15.0


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


def _envelope_with_intro_rule(pct: float = INTRO_DISCOUNT_PCT) -> dict:
    """The approved envelope plus an intro discount rule, so a cold bid carries a discount."""
    envelope = _envelope()
    envelope[INTRO_DISCOUNT_KEY] = pct
    return envelope


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

    granted = _hook_granted_identities(hook_claims)
    smuggled = {_claim_identity(c) for c in bid_claims} - granted
    assert not smuggled, f"these bid claims did not come from a tool hook: {sorted(smuggled)}"

    # ---- Harness amendment 6 (ESC-007): the same criterion at the DISCOUNT site. ----
    #
    # The context above is cold and the envelope defines no intro rule, so it produces no
    # discount at all — which is why this criterion passed for reasons that had nothing to do
    # with the discount site. `offer.discount` is the one claim-bearing node in a bid that has
    # no `key`, and since T-135 the boundary requires it to carry provenance, so it is exactly
    # the node most likely to be waved through or misread. Bid it for real.
    intro_context = _store_context(envelope=_envelope_with_intro_rule())
    intro_hooks = _build_hooks(ToolHooks, intro_context)
    discounted = make_bid(_bid_request("auc-0002"), intro_context, hooks=intro_hooks)

    offer = _first_value(_plain(discounted), "offer")
    discount = offer.get("discount") if isinstance(offer, dict) else None
    assert isinstance(discount, dict), (
        f"an envelope stating {INTRO_DISCOUNT_KEY}={INTRO_DISCOUNT_PCT} must produce a discounted "
        f"cold-start bid, or this criterion never reaches the discount site: {_plain(discounted)!r}"
    )
    provenance = discount.get("provenance")
    assert isinstance(provenance, dict), (
        "an emitted discount must cite the grant that authorized it by carrying that grant's "
        f"provenance: {discount!r}"
    )

    intro_hook_claims = _claims_in(getattr(intro_hooks, "emitted_claims", []))
    assert intro_hook_claims, "the hooks facade must expose the claims it emitted"
    intro_granted = _hook_granted_identities(intro_hook_claims)

    smuggled = {_claim_identity(c) for c in _claims_in(discounted)} - intro_granted
    assert not smuggled, (
        "a discount that cites a real grant — same depth, same provenance source and ref — "
        f"traces to a hook call and must not read as smuggled: {sorted(smuggled)}"
    )

    # And the other direction, which is the whole point of the criterion: a discount whose
    # provenance does NOT trace to a grant the hooks actually issued is still caught. Neither
    # forgery could be produced by `authorize_discount` — the first asks past the 20% cap, the
    # second cites a rule reference that was never minted.
    deeper_than_granted = dict(discount, value=float(discount["value"]) + 10.0)
    assert _claim_identity(deeper_than_granted) not in intro_granted, (
        "a discount deeper than the grant it cites must NOT trace to a hook call: "
        f"{deeper_than_granted!r}"
    )

    forged_ref = dict(
        discount, provenance=dict(provenance, ref=f"{provenance.get('ref')}-never-issued")
    )
    assert _claim_identity(forged_ref) not in intro_granted, (
        f"a discount citing a grant that was never issued must NOT trace to a hook call: "
        f"{forged_ref!r}"
    )

    fabricated = dict(
        discount,
        provenance=dict(provenance, source="seller_asserted", ref="whatever:never-minted"),
    )
    assert _claim_identity(fabricated) not in intro_granted, (
        "a discount whose provenance traces to no hook call at all must NOT trace to a hook "
        f"call: {fabricated!r}"
    )

    # And the unkeyed slot belongs to the GRANT alone. A discount that copies some OTHER real
    # hook emission's (value, source, ref) — the scraped list price, the pixel feed's stock
    # count — cites nothing that ever authorized a discount, and must not trace either. When
    # the unkeyed slot was lent to every hook claim instead, a 100%-off discount wearing the
    # scraped list-price claim's provenance verbatim read as fully traced.
    non_grant = [c for c in intro_hook_claims if _claim_identity(c)[0] != DISCOUNT_GRANT_KEY]
    assert non_grant, (
        "this auction must emit hook claims other than the discount grant, or the next check "
        "cannot bite"
    )
    for other in non_grant:
        _, value_json, source, ref = _claim_identity(other)
        piggyback = {
            "type": discount.get("type"),
            "value": json.loads(value_json),
            "provenance": dict(provenance, source=source, ref=ref),
        }
        assert _claim_identity(piggyback) not in intro_granted, (
            "a discount piggybacking on a hook emission that granted no discount must NOT "
            f"trace to a hook call: {piggyback!r}"
        )


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

#
# HARNESS AMENDMENT 1 (user-approved). The signing envelope below is REQUIRED, and the
# keyring is indexed `{signer_id: {key_id: secret}}` so a signer can hold more than one
# live key. Both reverse an earlier reading in which the *absence* of these fields from a
# frozen fixture was taken as the public contract. `sign_bid` and `receive_bid` still live
# in `packages.store_agent.src.external`; that placement is unchanged.

EXTERNAL_STORE = "store-external-1"
EXTERNAL_SIGNER = EXTERNAL_STORE
EXTERNAL_KEY_ID = "key-2026-01"
EXTERNAL_KEY = "external-secret-key-0001"
EXTERNAL_KEY_ID_ROTATED = "key-2026-07"
EXTERNAL_KEY_ROTATED = "external-secret-key-0002"

# A second signer that reuses the FIRST signer's key_id string under a different secret,
# so a lookup keyed on `key_id` alone cannot pass.
EXTERNAL_SIGNER_2 = "store-external-2"
EXTERNAL_KEY_ID_2 = EXTERNAL_KEY_ID
EXTERNAL_KEY_2 = "external-secret-key-0003"

ISSUED_AT = "2026-01-01T00:00:00Z"
NOW = "2026-01-01T00:00:05Z"          # issued_at + 5s — inside any sane freshness window
FAR_FUTURE = "2999-01-01T00:00:00Z"
AUCTION_DEADLINE = "2026-01-01T00:05:00Z"
BEFORE_DEADLINE = "2026-01-01T00:04:59Z"
AFTER_DEADLINE = "2026-01-01T00:05:01Z"
NONCE = "nonce-ext-0001"

REQUIRED_SIGNING_FIELDS = ("signer_id", "key_id", "issued_at", "nonce", "schema_version")


def _keyring() -> dict:
    """The amended keyring: two live keys for signer 1, one for signer 2."""
    return {
        EXTERNAL_SIGNER: {
            EXTERNAL_KEY_ID: EXTERNAL_KEY,
            EXTERNAL_KEY_ID_ROTATED: EXTERNAL_KEY_ROTATED,
        },
        EXTERNAL_SIGNER_2: {EXTERNAL_KEY_ID_2: EXTERNAL_KEY_2},
    }


def _external_payload(
    store_id: str = EXTERNAL_STORE,
    expires_at: str = FAR_FUTURE,
    *,
    signer_id=None,
    key_id: str = EXTERNAL_KEY_ID,
    issued_at: str = ISSUED_AT,
    nonce: str = NONCE,
    auction_id: str = "auc-0100",
    unit_price: float = 89.0,
) -> dict:
    return {
        "auction_id": auction_id,
        "store_id": store_id,
        "offer": {
            "product_ref": "ext-prod-1",
            "unit_price": unit_price,
            "total_price": unit_price,
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
        # --- required signing envelope (amendment 1) ---
        "signer_id": store_id if signer_id is None else signer_id,
        "key_id": key_id,
        "issued_at": issued_at,
        "nonce": nonce,
    }


def _present(receive_bid, payload, signature, *, nonce_store, keyring=None, now=NOW,
             auction_deadline=AUCTION_DEADLINE, **extra):
    """Offer one bid at the door. Returns (accepted, plain_result, queue)."""
    queue = _Recorder("queue")
    kwargs = dict(
        queue=queue,
        nonce_store=nonce_store,
        now=now,
        auction_deadline=auction_deadline,
    )
    kwargs.update(extra)
    ring = _keyring() if keyring is None else keyring
    try:
        result = receive_bid(payload, signature, ring, **kwargs)
    except Exception:  # noqa: BLE001 - raising is a valid way to refuse
        return False, None, queue
    plain = _plain(result)
    return _first_value(plain, "accepted") is True, plain, queue


def _accepts(receive_bid, payload, signature, **kwargs) -> bool:
    accepted, _plain_result, queue = _present(receive_bid, payload, signature, **kwargs)
    return accepted and queue.count == 1


def _rejects(receive_bid, payload, signature, **kwargs) -> bool:
    accepted, _plain_result, queue = _present(receive_bid, payload, signature, **kwargs)
    return (not accepted) and queue.count == 0


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_signed_external_bid_is_accepted_and_enqueued_for_verification():
    """R8/R18: a bid signed with a registered key is admitted as unverified and queued for verification."""
    _bootstrap_imports()
    from packages.store_agent.src.external import NonceStore, receive_bid, sign_bid

    payload = _external_payload()
    queue = _Recorder("verification_queue")

    result = receive_bid(
        payload,
        sign_bid(payload, EXTERNAL_KEY),
        _keyring(),
        queue=queue,
        nonce_store=NonceStore(),
        now=NOW,
        auction_deadline=AUCTION_DEADLINE,
    )
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
    assert NONCE in _canon(queue.payloads()), (
        "the queued work item must carry the submission's idempotency key (nonce) so "
        f"extraction + verification stay idempotent: {_plain(queue.payloads())!r}"
    )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_external_bid_with_a_bad_signature_is_rejected():
    """R8/C10: wrong, absent and replayed signatures — and expired or blacklisted bids — reject before enqueue."""
    _bootstrap_imports()
    from packages.store_agent.src.external import NonceStore, receive_bid, sign_bid

    good_payload = _external_payload()
    good_signature = sign_bid(good_payload, EXTERNAL_KEY)

    # Arm the test: the happy path must work, so a rejection below is a real
    # rejection rather than a TypeError from a mismatched call signature.
    assert _accepts(receive_bid, good_payload, good_signature, nonce_store=NonceStore()), (
        "control bid must be accepted and enqueued"
    )

    def rejects(payload, signature, **kwargs) -> bool:
        kwargs.setdefault("nonce_store", NonceStore())
        return _rejects(receive_bid, payload, signature, **kwargs)

    assert rejects(good_payload, sign_bid(good_payload, "wrong-key")), "wrong key must reject"
    assert rejects(good_payload, "not-a-signature"), "a garbage signature must reject"
    assert rejects(good_payload, None), "an absent signature must reject"
    assert rejects(good_payload, sign_bid(_external_payload(EXTERNAL_SIGNER_2), EXTERNAL_KEY)), (
        "a signature replayed from a different payload must reject"
    )

    expired = _external_payload(expires_at="2000-01-01T00:00:00Z")
    assert rejects(expired, sign_bid(expired, EXTERNAL_KEY)), (
        "an expired external offer must reject before enqueue"
    )
    assert rejects(good_payload, good_signature, blacklist=[EXTERNAL_STORE]), (
        "a blacklisted external store must reject before enqueue"
    )

    # An unregistered signer has no key to check against and must not be admitted.
    stranger = _external_payload("store-external-9")
    assert rejects(stranger, sign_bid(stranger, EXTERNAL_KEY)), (
        "a bid from a signer absent from the keyring must reject"
    )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_canonical_signing_bytes_are_deterministic_and_cover_every_signed_field():
    """C10: the signing input is a canonical form over auction, signer, issue time, nonce, key id,
    schema version and a payload hash — and every one of those changes it."""
    _bootstrap_imports()
    from packages.store_agent.src.external import canonical_signing_bytes, payload_hash

    payload = _external_payload()
    base = canonical_signing_bytes(payload)
    assert isinstance(base, (bytes, bytearray, str)) and len(base) > 0, (
        f"canonical_signing_bytes must return non-empty bytes, got {base!r}"
    )
    text = base.decode("utf-8") if isinstance(base, (bytes, bytearray)) else str(base)

    # Deterministic: same content, different dict insertion order, and a JSON round trip.
    assert canonical_signing_bytes(payload) == base, "canonicalization must be deterministic"
    reordered = dict(reversed(list(_external_payload().items())))
    assert canonical_signing_bytes(reordered) == base, (
        "canonicalization must not depend on mapping insertion order"
    )
    assert canonical_signing_bytes(json.loads(json.dumps(payload))) == base, (
        "canonicalization must survive a JSON round trip unchanged"
    )

    # The payload hash is a real digest over the bid body, and it rides in the signed bytes.
    digest = payload_hash(payload)
    assert isinstance(digest, str) and len(digest) >= 32, (
        f"payload_hash must be a digest string of at least 32 chars, got {digest!r}"
    )
    assert payload_hash(json.loads(json.dumps(payload))) == digest, "payload_hash must be stable"
    assert digest != payload_hash(_external_payload(unit_price=88.0)), (
        "payload_hash must change when the bid body changes — otherwise the signature "
        "does not cover the offer"
    )
    assert digest in text, (
        "the payload hash must appear in the canonical signing bytes so the signature "
        f"covers the body: {text!r}"
    )

    # Every covered field is present verbatim...
    for field in ("auction_id", "signer_id", "issued_at", "nonce", "key_id"):
        assert str(payload[field]) in text, (
            f"canonical signing bytes must cover {field!r}: {text!r}"
        )

    # ...and mutating ANY of them changes the signing input.
    mutations = {
        "auction_id": _external_payload(auction_id="auc-0999"),
        "signer_id": _external_payload(signer_id=EXTERNAL_SIGNER_2),
        "store_id": _external_payload(EXTERNAL_SIGNER_2, signer_id=EXTERNAL_SIGNER),
        "issued_at": _external_payload(issued_at="2026-01-01T00:00:01Z"),
        "nonce": _external_payload(nonce="nonce-ext-0002"),
        "key_id": _external_payload(key_id=EXTERNAL_KEY_ID_ROTATED),
        "offer.unit_price": _external_payload(unit_price=88.0),
    }
    for field, mutated in mutations.items():
        assert canonical_signing_bytes(mutated) != base, (
            f"changing {field} must change the canonical signing bytes — it is a covered field"
        )
    schema_v2 = dict(payload, schema_version="2")
    assert canonical_signing_bytes(schema_v2) != base, (
        "changing schema_version must change the canonical signing bytes"
    )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_external_bid_missing_any_required_signing_field_is_rejected():
    """C10: signer_id, key_id, issued_at, nonce and schema_version are required, not optional."""
    _bootstrap_imports()
    from packages.store_agent.src.external import NonceStore, receive_bid, sign_bid

    complete = _external_payload()
    assert _accepts(
        receive_bid, complete, sign_bid(complete, EXTERNAL_KEY), nonce_store=NonceStore()
    ), "control: the complete envelope must be accepted, or the rejections below prove nothing"

    for field in REQUIRED_SIGNING_FIELDS:
        broken = _external_payload(nonce=f"nonce-missing-{field}")
        del broken[field]
        try:
            signature = sign_bid(broken, EXTERNAL_KEY)
        except Exception:  # noqa: BLE001 - refusing to sign an incomplete envelope is fine
            signature = "unsignable"
        assert _rejects(receive_bid, broken, signature, nonce_store=NonceStore()), (
            f"an external bid missing required signing field {field!r} must be rejected "
            "before enqueue — this field is no longer optional"
        )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_signing_metadata_is_covered_by_the_signature():
    """C10: the envelope fields are inside the signed bytes — tampering with one after signing rejects."""
    _bootstrap_imports()
    from packages.store_agent.src.external import NonceStore, receive_bid, sign_bid

    original = _external_payload()
    signature = sign_bid(original, EXTERNAL_KEY)
    assert _accepts(receive_bid, original, signature, nonce_store=NonceStore()), (
        "control: the untampered bid must be accepted with its own signature"
    )

    tampered = {
        # each of these keeps the bid otherwise valid: still fresh, still inside the
        # deadline, still a registered signer — so only the signature can reject it.
        "auction_id": _external_payload(auction_id="auc-0999"),
        "issued_at": _external_payload(issued_at="2026-01-01T00:00:01Z"),
        "nonce": _external_payload(nonce="nonce-ext-0002"),
        "key_id": _external_payload(key_id=EXTERNAL_KEY_ID_ROTATED),
        "signer_id": _external_payload(EXTERNAL_SIGNER_2, key_id=EXTERNAL_KEY_ID_2),
        "offer.unit_price": _external_payload(unit_price=1.0),
    }
    for field, mutated in tampered.items():
        assert _rejects(receive_bid, mutated, signature, nonce_store=NonceStore()), (
            f"a bid whose {field} was changed after signing must reject — the field is signed"
        )


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_replayed_nonce_is_rejected_and_consumption_persists_until_the_auction_deadline():
    """C10: a nonce is single-use per signer, remembered until after the auction deadline."""
    _bootstrap_imports()
    from packages.store_agent.src.external import NonceStore, receive_bid, sign_bid

    store = NonceStore()
    payload = _external_payload()
    signature = sign_bid(payload, EXTERNAL_KEY)

    assert _accepts(receive_bid, payload, signature, nonce_store=store), (
        "control: the first presentation of a fresh nonce must be accepted and enqueued"
    )
    assert store.seen(EXTERNAL_SIGNER, NONCE) is True, (
        "an accepted submission must consume its nonce in the injected store"
    )

    assert _rejects(receive_bid, payload, signature, nonce_store=store), (
        "the same nonce presented a second time must be rejected before enqueue (replay)"
    )

    # The store is not simply wedged shut: a new nonce from the same signer still passes...
    second = _external_payload(nonce="nonce-ext-0002")
    assert _accepts(receive_bid, second, sign_bid(second, EXTERNAL_KEY), nonce_store=store), (
        "a different nonce from the same signer must still be accepted"
    )
    # ...and nonce uniqueness is scoped per signer, not global.
    other_signer = _external_payload(EXTERNAL_SIGNER_2, key_id=EXTERNAL_KEY_ID_2)
    assert _accepts(
        receive_bid, other_signer, sign_bid(other_signer, EXTERNAL_KEY_2), nonce_store=store
    ), "the same nonce string from a DIFFERENT signer must be accepted — scoping is per signer"

    # Consumption persists until after the auction deadline.
    store.purge_expired(BEFORE_DEADLINE)
    assert store.seen(EXTERNAL_SIGNER, NONCE) is True, (
        "a consumed nonce must still be remembered at any moment before the auction deadline"
    )
    store.purge_expired(AFTER_DEADLINE)
    assert store.seen(EXTERNAL_SIGNER, NONCE) is False, (
        "nonce retention ends only after the auction deadline has passed"
    )

    # A submission arriving after the auction deadline is refused outright.
    late = _external_payload(nonce="nonce-ext-0003", issued_at=AUCTION_DEADLINE)
    assert _rejects(
        receive_bid,
        late,
        sign_bid(late, EXTERNAL_KEY),
        nonce_store=NonceStore(),
        now=AFTER_DEADLINE,
        auction_deadline=AUCTION_DEADLINE,
    ), "a bid submitted after the auction deadline must reject before enqueue"


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_keyring_supports_key_rotation_and_selects_the_key_by_key_id():
    """C10: `{signer_id: {key_id: secret}}` — a signer holds several live keys and the
    envelope's key_id chooses exactly one of them."""
    _bootstrap_imports()
    from packages.store_agent.src.external import NonceStore, receive_bid, sign_bid

    old_key_bid = _external_payload(key_id=EXTERNAL_KEY_ID, nonce="nonce-rot-1")
    new_key_bid = _external_payload(key_id=EXTERNAL_KEY_ID_ROTATED, nonce="nonce-rot-2")

    # Both keys are live at once — that is what makes rotation possible without downtime.
    assert _accepts(
        receive_bid, old_key_bid, sign_bid(old_key_bid, EXTERNAL_KEY), nonce_store=NonceStore()
    ), "the signer's first registered key must authenticate"
    assert _accepts(
        receive_bid,
        new_key_bid,
        sign_bid(new_key_bid, EXTERNAL_KEY_ROTATED),
        nonce_store=NonceStore(),
    ), "the signer's rotated key must authenticate too — the keyring holds more than one"

    # The key_id selects the secret; the receiver must not try every key it holds.
    assert _rejects(
        receive_bid, new_key_bid, sign_bid(new_key_bid, EXTERNAL_KEY), nonce_store=NonceStore()
    ), "a bid stamped key_id=rotated but signed with the OLD secret must reject"
    assert _rejects(
        receive_bid,
        old_key_bid,
        sign_bid(old_key_bid, EXTERNAL_KEY_ROTATED),
        nonce_store=NonceStore(),
    ), "a bid stamped key_id=old but signed with the ROTATED secret must reject"

    unknown = _external_payload(key_id="key-not-registered", nonce="nonce-rot-3")
    assert _rejects(
        receive_bid, unknown, sign_bid(unknown, EXTERNAL_KEY), nonce_store=NonceStore()
    ), "an unregistered key_id must reject even when the signature matches a live secret"

    # key_id is scoped by signer: signer 2 reuses signer 1's key_id string with its own secret.
    cross = _external_payload(EXTERNAL_SIGNER_2, key_id=EXTERNAL_KEY_ID_2, nonce="nonce-rot-4")
    assert _rejects(
        receive_bid, cross, sign_bid(cross, EXTERNAL_KEY), nonce_store=NonceStore()
    ), "signer 2's key_id must not resolve to signer 1's secret"
    assert _accepts(
        receive_bid, cross, sign_bid(cross, EXTERNAL_KEY_2), nonce_store=NonceStore()
    ), "signer 2's own secret for that key_id must authenticate"

    # The pre-amendment flat `{store_id: secret}` keyring cannot express key selection and
    # must not be honoured as a legacy shortcut on the public path.
    assert _rejects(
        receive_bid,
        old_key_bid,
        sign_bid(old_key_bid, EXTERNAL_KEY),
        keyring={EXTERNAL_SIGNER: EXTERNAL_KEY},
        nonce_store=NonceStore(),
    ), "a flat {signer_id: secret} keyring must not authenticate — key_id selection is required"


@pytest.mark.epic("E4")
@pytest.mark.ticket("T-044")
def test_issued_at_outside_the_freshness_window_is_rejected():
    """C10: a correctly signed bid is still refused when its issue time is stale or future-dated."""
    _bootstrap_imports()
    from packages.store_agent.src.external import NonceStore, receive_bid, sign_bid

    def present(payload, **kwargs):
        kwargs.setdefault("nonce_store", NonceStore())
        kwargs.setdefault("auction_deadline", FAR_FUTURE)
        return _present(receive_bid, payload, sign_bid(payload, EXTERNAL_KEY), **kwargs)

    fresh = _external_payload(nonce="nonce-fresh-1")
    accepted, _result, queue = present(fresh, now=NOW)
    assert accepted and queue.count == 1, (
        "control: a bid issued 5 seconds ago must be accepted under the default window"
    )

    stale_accepted, _r, stale_queue = present(fresh, now="2026-01-31T00:00:00Z")
    assert not stale_accepted and stale_queue.count == 0, (
        "a bid issued 30 days before `now` must reject — the freshness window is finite"
    )

    future_accepted, _r2, future_queue = present(fresh, now="2025-12-31T00:00:00Z")
    assert not future_accepted and future_queue.count == 0, (
        "a bid issued a day in the FUTURE relative to `now` must reject — clock-skew tolerance "
        "is bounded too"
    )

    # The window is enforced, not merely present: with an explicit 300s window, +120s is in
    # and +600s is out.
    inside_accepted, _r3, inside_queue = present(
        _external_payload(nonce="nonce-fresh-2"),
        now="2026-01-01T00:02:00Z",
        freshness_window_seconds=300,
    )
    assert inside_accepted and inside_queue.count == 1, (
        "an issued_at 120s before `now` must be accepted under a 300s freshness window"
    )
    outside_accepted, _r4, outside_queue = present(
        _external_payload(nonce="nonce-fresh-3"),
        now="2026-01-01T00:10:00Z",
        freshness_window_seconds=300,
    )
    assert not outside_accepted and outside_queue.count == 0, (
        "an issued_at 600s before `now` must reject under a 300s freshness window"
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
