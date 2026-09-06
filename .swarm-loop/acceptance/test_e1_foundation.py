"""Epic E1 — Foundation: contracts, the dual-path bid boundary, embeddings, LLM config.

Covers the SPEC surface that E1 owns:

* **R8 / S5** — the dual-path bid boundary. A hosted-path bid whose claims did not come
  through a provenance tool hook is rejected; the identical bid on the external path is
  admitted and flagged for verification (R18); a claim with no provenance is rejected on
  every path; expired offers and blacklisted stores reject on every path.
* **R19** — hard constraints are eligibility *filters* (`field/op/value`, never weighted)
  and preferences are *scores* (`field/direction/weight`).
* **C1** — every pinned protocol object of DESIGN §Interfaces exists in
  `packages.contracts` and round-trips through serialization.
* **C2 / C4** — the `HashEmbedding` double is deterministic and 1024-dimensional behind
  the `EmbeddingProvider` interface (D6 pins 1024/cosine).
* **C4 / C9 / D3** — per-role LLM model ids come from the environment, not from code, and
  the deterministic LLM double answers with the network disabled.

Authoring rules (see `.swarm-loop/acceptance/README.md`): every product import happens
INSIDE the test body, and every test carries `epic` + `ticket` markers. Module scope holds
stdlib and pytest only.
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
import socket

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# --- tiny shape helpers (no product code touched) ----------------------------------
def _has(obj, key: str) -> bool:
    """True when `obj` exposes `key`, whether it is a mapping or a model object."""
    try:
        obj[key]  # type: ignore[index]
        return True
    except Exception:
        pass
    return hasattr(obj, key)


def _get(obj, key: str):
    """Read `key` off a mapping or a model object; raise AssertionError if absent."""
    try:
        return obj[key]  # type: ignore[index]
    except Exception:
        pass
    if hasattr(obj, key):
        return getattr(obj, key)
    raise AssertionError(f"expected {key!r} on {obj!r}")


def _dump(model):
    """Serialize a generated protocol model to plain data."""
    for attr in ("model_dump", "dict"):
        fn = getattr(model, attr, None)
        if callable(fn):
            return fn()
    import dataclasses

    if dataclasses.is_dataclass(model):
        return dataclasses.asdict(model)
    raise AssertionError(f"{type(model).__name__} exposes no serialization surface")


def _load(cls, data):
    """Deserialize plain data back into a generated protocol model."""
    for attr in ("model_validate", "parse_obj"):
        fn = getattr(cls, attr, None)
        if callable(fn):
            return fn(data)
    return cls(**data)


def _probe(model, path: str):
    """Read a dotted `path` off a loaded model and normalize the leaf to a primitive.

    Integer segments index into a sequence; everything else is a field read through
    `_get`, so the same probe works against pydantic models, dataclasses and dicts.
    Enum members are unwrapped to their `.value` — the wire value is the contract.
    """
    cursor = model
    for segment in path.split("."):
        if segment.isdigit():
            cursor = list(cursor)[int(segment)]
        else:
            cursor = _get(cursor, segment)
    return getattr(cursor, "value", cursor)


def _rejects(cls, payload) -> bool:
    """True when the model type refuses `payload` instead of quietly accepting it."""
    try:
        _load(cls, payload)
    except Exception:
        return True
    return False


class _NetworkBlocked(RuntimeError):
    """Raised by the socket stubs in the offline test; never raised by product code."""


def _block_network(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise _NetworkBlocked("the test disabled the network; product code reached for it")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


# --- wire-shaped fixtures, per DESIGN §Interfaces ----------------------------------
_HOOK_PROVENANCE = {
    "source": "owner_statement",
    "ref": "envelope:store-1:v3#commitment-2",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 1,
}
_ASSERTED_PROVENANCE = {
    "source": "seller_asserted",
    "ref": "pitch:p-1#span-4",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 5,
}

_NOT_EXPIRED = "2999-01-01T00:00:00Z"
_LONG_EXPIRED = "2000-01-01T00:00:00Z"


def _claim(key: str, value, provenance) -> dict:
    claim = {"key": key, "value": value}
    if provenance is not None:
        claim["provenance"] = provenance
    return claim


def _offer(expires_at: str = _NOT_EXPIRED) -> dict:
    return {
        "product_ref": "prod-1",
        "unit_price": 49.0,
        "discount": {"type": "percentage", "value": 10.0, "provenance": _HOOK_PROVENANCE},
        "commitments": [],
        "total_price": 44.1,
        "expires_at": expires_at,
        "checkout_url": "https://store-one.example.com/cart/1:1",
    }


#: The catalog `_offer` is written against: `prod-1` lists at the 49.00 the offer itself
#: carries, with a 25% ceiling that is deeper than the 10% the offer declares. So the price
#: wall stays SILENT and each test below is refused only for its own subject.
#:
#: ADDED BY AMENDMENT (ESC-029), and it completes an INPUT — it changes no assertion. These
#: tests were written when an absent `list_prices` meant "abstain", so they passed no roster
#: and the wall never fired. T-306 made the absent roster REFUSE, because a signed bid
#: awarding itself 85% off was being admitted whenever the roster was omitted. Without this
#: the wall refuses these bids before they reach the provenance, expiry and blacklist rules
#: they exist to grade, and every `ok is True` control below fails for a reason it never
#: meant to test.
_FIXTURE_ROSTER = {"prod-1": {"list_price": 49.0, "max_discount_pct": 25.0}}


def _bid(claims, store_id: str = "store-1", expires_at: str = _NOT_EXPIRED) -> dict:
    return {
        "auction_id": "auc-1",
        "store_id": store_id,
        "offer": _offer(expires_at),
        "claims": list(claims),
        "message": None,
        "agent_version": "store-agent/1.0.0",
        "signature": "sig-deadbeef",
        "schema_version": "1.0.0",
    }


def _trust_snapshot() -> dict:
    """`store_id -> TrustSnapshot`-shaped mapping handed to the boundary validator."""
    return {
        "store-1": {"store_id": "store-1", "score": 0.6, "blacklisted": False},
        "store-bad": {"store_id": "store-bad", "score": 0.05, "blacklisted": True},
    }


def _intent_dict(hard_constraints=None, preferences=None) -> dict:
    return {
        "intent_id": "int-1",
        "cluster_id": "cluster-serum",
        "query": "gentle vitamin C serum for sensitive skin",
        "category": "skincare",
        "hard_constraints": (
            [{"field": "fragrance_free", "op": "eq", "value": True}]
            if hard_constraints is None
            else hard_constraints
        ),
        "preferences": (
            [{"field": "price", "direction": "minimize", "weight": 0.6}]
            if preferences is None
            else preferences
        ),
        "ship_to": "US-CA",
        "currency": "USD",
        "budget_band": "40-80",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


_PINNED_PROTOCOL_OBJECTS = (
    "Intent",
    "BuyerProfile",
    "BidRequest",
    "Claim",
    "Provenance",
    "Offer",
    "Bid",
    "VerificationResult",
    "Shortlist",
    "LossReport",
    "LedgerEvent",
    "TrustSnapshot",
    "TrustEventPayload",
    "Envelope",
)

_PROFILE = {
    "pseudonym": "psn-e1-0001",
    "buckets": {
        "budget_band": "40-80",
        "category_affinity": ["skincare"],
        "frequency_tier": "occasional",
        "region": "US-CA",
        "first_time": False,
    },
}

_LEDGER_EVENT = {
    "event_id": "ev-1",
    "ts": "2026-01-01T00:00:00Z",
    "kind": "feedback",
    "auction_id": "auc-1",
    "store_id": "store-1",
    "order_ref": "ord-1",
    "payload": {"matched_pitch": False, "reason": "ships_within missed"},
}


def _protocol_payloads() -> dict:
    """A realistic wire payload per pinned protocol object, plus the fields it must keep.

    Every payload is the object as DESIGN §Interfaces spells it, using the same field
    names and value shapes the rest of the frozen suite hands to the product. The second
    element of each pair is a `{dotted path: expected primitive}` map of fields that must
    survive construction and a serialize/deserialize round trip unchanged. Only
    coercion-stable primitives are probed (ids, enums, numbers, booleans) so a model that
    parses timestamps into datetimes is not penalised for doing so.
    """
    commitment = _claim("free_returns", "30 days", _HOOK_PROVENANCE)
    return {
        "Intent": (
            _intent_dict(),
            {
                "intent_id": "int-1",
                "cluster_id": "cluster-serum",
                "category": "skincare",
                "ship_to": "US-CA",
                "currency": "USD",
                "budget_band": "40-80",
                "hard_constraints.0.field": "fragrance_free",
                "hard_constraints.0.op": "eq",
                "preferences.0.field": "price",
                "preferences.0.direction": "minimize",
                "preferences.0.weight": 0.6,
            },
        ),
        "BuyerProfile": (
            _PROFILE,
            {
                "pseudonym": "psn-e1-0001",
                "buckets.budget_band": "40-80",
                "buckets.region": "US-CA",
                "buckets.first_time": False,
            },
        ),
        "BidRequest": (
            {
                "auction_id": "auc-1",
                "intent": _intent_dict(),
                "profile": _PROFILE,
                "respond_by": "2999-01-01T00:00:00Z",
            },
            {
                "auction_id": "auc-1",
                "intent.intent_id": "int-1",
                "intent.preferences.0.weight": 0.6,
                "profile.pseudonym": "psn-e1-0001",
                "profile.buckets.first_time": False,
            },
        ),
        "Claim": (
            commitment,
            {
                "key": "free_returns",
                "value": "30 days",
                "provenance.source": "owner_statement",
                "provenance.authority_rank": 1,
            },
        ),
        "Provenance": (
            _HOOK_PROVENANCE,
            {
                "source": "owner_statement",
                "ref": "envelope:store-1:v3#commitment-2",
                "authority_rank": 1,
            },
        ),
        "Offer": (
            _offer(),
            {
                "product_ref": "prod-1",
                "unit_price": 49.0,
                "total_price": 44.1,
                "discount.type": "percentage",
                "discount.value": 10.0,
                "discount.provenance.source": "owner_statement",
            },
        ),
        "Bid": (
            _bid([commitment]),
            {
                "auction_id": "auc-1",
                "store_id": "store-1",
                "agent_version": "store-agent/1.0.0",
                "signature": "sig-deadbeef",
                "schema_version": "1.0.0",
                "offer.product_ref": "prod-1",
                "offer.total_price": 44.1,
                "claims.0.key": "free_returns",
                "claims.0.provenance.source": "owner_statement",
            },
        ),
        "VerificationResult": (
            {
                "pitch_ref": "pitch:p-1",
                "catalog_snapshot": "snapshot://store-1@sha256:0f1e2d3c",
                "claims": [
                    {
                        "claim_ref": "claim-1",
                        "status": "verified",
                        "observed_value": "30 days",
                        "evidence_refs": ["snapshot://store-1/policies/returns"],
                        "confidence": 0.92,
                    },
                    {
                        "claim_ref": "claim-2",
                        "status": "unsupported",
                        "evidence_refs": [],
                        "confidence": 0.10,
                    },
                ],
                "verified_claim_ratio": 0.5,
                "verifier_version": "verification/1.0.0",
            },
            {
                "pitch_ref": "pitch:p-1",
                "catalog_snapshot": "snapshot://store-1@sha256:0f1e2d3c",
                "verified_claim_ratio": 0.5,
                "verifier_version": "verification/1.0.0",
                "claims.0.claim_ref": "claim-1",
                "claims.0.status": "verified",
                "claims.0.confidence": 0.92,
                "claims.1.status": "unsupported",
            },
        ),
        "Shortlist": (
            {
                "auction_id": "auc-1",
                "slots": [
                    {
                        "slot": "fit",
                        "bid_ref": "bid-1",
                        "fit_score": 0.82,
                        "trust_summary": {"score": 0.62, "confidence": 0.4},
                        "provenance_labels": ["store_confirmed"],
                    },
                    {
                        "slot": "value",
                        "bid_ref": "bid-2",
                        "fit_score": 0.71,
                        "trust_summary": {"score": 0.55, "confidence": 0.3},
                        "provenance_labels": ["from_their_website"],
                    },
                ],
            },
            {
                "auction_id": "auc-1",
                "slots.0.slot": "fit",
                "slots.0.bid_ref": "bid-1",
                "slots.0.fit_score": 0.82,
                "slots.0.trust_summary.score": 0.62,
                "slots.1.slot": "value",
                "slots.1.bid_ref": "bid-2",
            },
        ),
        "LossReport": (
            {
                "store_id": "store-1",
                "window": {"start": 1767225600.0, "end": 1767830400.0},
                "by_cluster": [
                    {
                        "cluster_id": "cluster-serum",
                        "lost": 3,
                        "reasons": {"fit": 2, "price": 1, "commitments": 0, "trust": 0},
                        "unmet_criteria": ["fragrance_free == True"],
                    }
                ],
            },
            {
                "store_id": "store-1",
                "by_cluster.0.cluster_id": "cluster-serum",
                "by_cluster.0.lost": 3,
                "by_cluster.0.reasons.fit": 2,
                "by_cluster.0.reasons.price": 1,
                "by_cluster.0.unmet_criteria.0": "fragrance_free == True",
            },
        ),
        "LedgerEvent": (
            _LEDGER_EVENT,
            {
                "event_id": "ev-1",
                "kind": "feedback",
                "auction_id": "auc-1",
                "store_id": "store-1",
                "order_ref": "ord-1",
                "payload.matched_pitch": False,
                "payload.reason": "ships_within missed",
            },
        ),
        "TrustSnapshot": (
            {
                "store_id": "store-1",
                "score": 0.62,
                "dims": {
                    dim: {"alpha": 2.0, "beta": 1.0, "decayed_at": "2026-01-01T00:00:00Z"}
                    for dim in (
                        "price_honored",
                        "discount_honored",
                        "shipped_on_time",
                        "not_returned",
                        "feedback_match",
                        # R12 amendment: product-fact verification is its own trust
                        # dimension, not a transaction dimension wearing a disguise.
                        "catalog_claim_accuracy",
                    )
                },
                "blacklisted": False,
            },
            {
                "store_id": "store-1",
                "score": 0.62,
                "blacklisted": False,
                "dims.price_honored.alpha": 2.0,
                "dims.feedback_match.beta": 1.0,
                "dims.catalog_claim_accuracy.alpha": 2.0,
                "dims.catalog_claim_accuracy.beta": 1.0,
            },
        ),
        "TrustEventPayload": (
            {
                "store_id": "store-1",
                "event": _LEDGER_EVENT,
                "dim": "shipped_on_time",
                "delta": -0.35,
                "pseudonymous_context": {
                    "cluster_id": "cluster-serum",
                    "pseudonym": "psn-e1-0001",
                },
            },
            {
                "store_id": "store-1",
                "dim": "shipped_on_time",
                "delta": -0.35,
                "event.event_id": "ev-1",
                "event.kind": "feedback",
                "pseudonymous_context.cluster_id": "cluster-serum",
            },
        ),
        "Envelope": (
            {
                "store_id": "store-1",
                "version": 3,
                "floors": [{"product_ref": "prod-1", "min_price": 30.0}],
                "max_discount_pct": 20.0,
                "budget_cap": 500.0,
                "pursue_clusters": ["cluster-serum"],
                "standing_commitments": [commitment],
                "activation": "shadow",
            },
            {
                "store_id": "store-1",
                "version": 3,
                "max_discount_pct": 20.0,
                "budget_cap": 500.0,
                "activation": "shadow",
                "floors.0.product_ref": "prod-1",
                "floors.0.min_price": 30.0,
                "pursue_clusters.0": "cluster-serum",
                "standing_commitments.0.key": "free_returns",
                "standing_commitments.0.provenance.source": "owner_statement",
            },
        ),
    }


# ===================================================================================
# T-010 — contracts and the dual-path bid boundary
# ===================================================================================
@pytest.mark.epic("E1")
@pytest.mark.ticket("T-010")
def test_contracts_expose_every_pinned_protocol_object():
    """C1 / DESIGN §Interfaces: every pinned protocol object is a real model type.

    Presence is not the contract. Each pinned name must be a type that *validates*: it
    accepts the object as DESIGN spells it, keeps every pinned field through a
    dump/load round trip, and refuses payloads that are not that object. A package of
    bodiless classes exposes all fourteen names and satisfies none of this.
    """
    import packages.contracts as contracts

    missing = [name for name in _PINNED_PROTOCOL_OBJECTS if not hasattr(contracts, name)]
    assert missing == [], f"packages.contracts is missing pinned objects: {missing}"

    not_a_type = [
        name
        for name in _PINNED_PROTOCOL_OBJECTS
        if not inspect.isclass(getattr(contracts, name))
    ]
    assert not_a_type == [], f"pinned objects that are not model types: {not_a_type}"

    payloads = _protocol_payloads()
    assert sorted(payloads) == sorted(_PINNED_PROTOCOL_OBJECTS), (
        "the fixture must cover exactly the pinned objects; "
        f"missing {sorted(set(_PINNED_PROTOCOL_OBJECTS) - set(payloads))}, "
        f"extra {sorted(set(payloads) - set(_PINNED_PROTOCOL_OBJECTS))}"
    )

    for name in _PINNED_PROTOCOL_OBJECTS:
        cls = getattr(contracts, name)
        payload, expected_fields = payloads[name]

        try:
            model = _load(cls, payload)
        except Exception as exc:  # noqa: BLE001 — any refusal is the failure
            raise AssertionError(
                f"{name} rejected its DESIGN §Interfaces payload {payload!r}: {exc!r}"
            ) from None

        for path, expected in expected_fields.items():
            actual = _probe(model, path)
            assert actual == expected, (
                f"{name}.{path} did not survive construction: "
                f"expected {expected!r}, got {actual!r}"
            )

        # Round trip: serialize, deserialize, and the pinned fields are still there.
        dumped = _dump(model)
        try:
            reloaded = _load(cls, dumped)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"{name} does not round-trip its own serialization: {exc!r}"
            ) from None
        for path, expected in expected_fields.items():
            actual = _probe(reloaded, path)
            assert actual == expected, (
                f"{name}.{path} was lost or mangled by the serialization round trip: "
                f"expected {expected!r}, got {actual!r}"
            )

        # A schema type requires its fields. An empty payload is not this object.
        assert _rejects(cls, {}), (
            f"{name} accepted an empty payload — it validates nothing, so it is not a "
            "generated schema type"
        )

    # Closed enums are closed: a value outside the pinned set is a schema error, not a
    # free-text field that rides through into the graph or the envelope.
    assert _rejects(
        contracts.Provenance, {**_HOOK_PROVENANCE, "source": "made-up-source"}
    ), "Provenance.source must be the closed enum DESIGN pins, not free text"
    envelope_payload, _ = payloads["Envelope"]
    assert _rejects(contracts.Envelope, {**envelope_payload, "activation": "whenever"}), (
        "Envelope.activation must be the closed enum shadow|active|killed"
    )


@pytest.mark.epic("E1")
@pytest.mark.ticket("T-010")
def test_hosted_bid_with_a_seller_asserted_claim_is_rejected():
    """R8 / S5: a hosted-path bid carrying a seller_asserted claim is rejected."""
    from packages.contracts import validate_bid

    asserted = _bid([_claim("spf", 30, _ASSERTED_PROVENANCE)])
    rejected = validate_bid(asserted, path="hosted", trust_snapshot=_trust_snapshot(), list_prices=_FIXTURE_ROSTER)
    assert _get(rejected, "ok") is False
    assert list(_get(rejected, "reasons")), "a rejection must say why it rejected"

    # Control: the same bid with a hook-provenanced claim is admitted on the hosted path,
    # so the test cannot pass by rejecting everything.
    hooked = _bid([_claim("spf", 30, _HOOK_PROVENANCE)])
    admitted = validate_bid(hooked, path="hosted", trust_snapshot=_trust_snapshot(), list_prices=_FIXTURE_ROSTER)
    assert _get(admitted, "ok") is True, _get(admitted, "reasons")


@pytest.mark.epic("E1")
@pytest.mark.ticket("T-010")
def test_external_bid_with_the_same_claim_is_admitted_and_flagged_unverified():
    """R8 / R18: the identical seller_asserted claim admits on the external path, flagged."""
    from packages.contracts import validate_bid

    asserted = _bid([_claim("spf", 30, _ASSERTED_PROVENANCE)])
    result = validate_bid(asserted, path="external", trust_snapshot=_trust_snapshot(), list_prices=_FIXTURE_ROSTER)
    assert _get(result, "ok") is True, _get(result, "reasons")
    assert _get(result, "requires_verification") is True

    # Control: an external bid whose claims are all hook-provenanced needs no verification,
    # so the flag tracks the claim's provenance rather than the path alone.
    hooked = validate_bid(
        _bid([_claim("spf", 30, _HOOK_PROVENANCE)]),
        path="external",
        trust_snapshot=_trust_snapshot(), list_prices=_FIXTURE_ROSTER,
    )
    assert _get(hooked, "ok") is True, _get(hooked, "reasons")
    assert _get(hooked, "requires_verification") is False


@pytest.mark.epic("E1")
@pytest.mark.ticket("T-010")
def test_a_claim_with_no_provenance_is_rejected_on_every_path():
    """S5: a claim with absent or empty provenance is rejected on hosted and external."""
    from packages.contracts import validate_bid

    no_provenance = _bid([_claim("spf", 30, None)])
    empty_source = _bid(
        [_claim("spf", 30, {**_HOOK_PROVENANCE, "source": ""})]
    )

    for path in ("hosted", "external"):
        for bid in (no_provenance, empty_source):
            result = validate_bid(bid, path=path, trust_snapshot=_trust_snapshot(), list_prices=_FIXTURE_ROSTER)
            assert _get(result, "ok") is False, (
                f"unprovenanced claim admitted on path={path}: {bid['claims']}"
            )
            assert list(_get(result, "reasons"))

    # Control: identical bid with a well-formed provenance record is admitted on both paths.
    for path in ("hosted", "external"):
        ok = validate_bid(
            _bid([_claim("spf", 30, _HOOK_PROVENANCE)]),
            path=path,
            trust_snapshot=_trust_snapshot(), list_prices=_FIXTURE_ROSTER,
        )
        assert _get(ok, "ok") is True, _get(ok, "reasons")


@pytest.mark.epic("E1")
@pytest.mark.ticket("T-010")
def test_expired_or_blacklisted_bids_reject_on_both_paths():
    """R8 / R12: an expired offer and a blacklisted store reject on hosted and external."""
    from packages.contracts import validate_bid

    snapshot = _trust_snapshot()
    expired = _bid([_claim("spf", 30, _HOOK_PROVENANCE)], expires_at=_LONG_EXPIRED)
    blacklisted = _bid([_claim("spf", 30, _HOOK_PROVENANCE)], store_id="store-bad")

    for path in ("hosted", "external"):
        expired_result = validate_bid(expired, path=path, trust_snapshot=snapshot, list_prices=_FIXTURE_ROSTER)
        assert _get(expired_result, "ok") is False, f"expired offer admitted on path={path}"
        assert list(_get(expired_result, "reasons"))

        blacklist_result = validate_bid(blacklisted, path=path, trust_snapshot=snapshot, list_prices=_FIXTURE_ROSTER)
        assert _get(blacklist_result, "ok") is False, (
            f"blacklisted store admitted on path={path}"
        )
        assert list(_get(blacklist_result, "reasons"))

        # Control: unexpired offer from a non-blacklisted store is admitted on this path.
        live = validate_bid(
            _bid([_claim("spf", 30, _HOOK_PROVENANCE)]), path=path, trust_snapshot=snapshot, list_prices=_FIXTURE_ROSTER
        )
        assert _get(live, "ok") is True, _get(live, "reasons")


@pytest.mark.epic("E1")
@pytest.mark.ticket("T-010")
def test_hard_constraints_are_filters_and_preferences_are_scores():
    """R19: hard constraints carry field/op/value and no weight; preferences carry weight."""
    from packages.contracts import Intent

    intent = Intent(**_intent_dict())

    constraint = list(_get(intent, "hard_constraints"))[0]
    assert _get(constraint, "field") == "fragrance_free"
    assert _get(constraint, "op") == "eq"
    assert _get(constraint, "value") is True
    assert not _has(constraint, "weight"), "a hard constraint is a filter, never a score term"

    preference = list(_get(intent, "preferences"))[0]
    assert _get(preference, "field") == "price"
    assert _get(preference, "direction") == "minimize"
    assert float(_get(preference, "weight")) == pytest.approx(0.6)

    # A weighted hard constraint is a schema error, not a silently-dropped field.
    with pytest.raises(Exception):
        Intent(
            **_intent_dict(
                hard_constraints=[
                    {"field": "fragrance_free", "op": "eq", "value": True, "weight": 0.9}
                ]
            )
        )

    # The two enums are closed sets.
    with pytest.raises(Exception):
        Intent(
            **_intent_dict(
                hard_constraints=[{"field": "name", "op": "regex", "value": "^serum"}]
            )
        )
    with pytest.raises(Exception):
        Intent(
            **_intent_dict(
                preferences=[{"field": "price", "direction": "sideways", "weight": 0.5}]
            )
        )


@pytest.mark.epic("E1")
@pytest.mark.ticket("T-010")
def test_protocol_objects_round_trip_serialization():
    """C1: Bid and LedgerEvent survive dump -> JSON -> load with equality intact."""
    from packages.contracts import Bid, LedgerEvent

    bid = Bid(**_bid([_claim("spf", 30, _HOOK_PROVENANCE)]))
    dumped = _dump(bid)

    # Nested Offer / discount / Provenance must survive the dump, not collapse to a repr.
    nested_source = _get(
        _get(_get(_get(dumped, "offer"), "discount"), "provenance"), "source"
    )
    assert nested_source == "owner_statement"
    assert _get(_get(dumped, "offer"), "product_ref") == "prod-1"
    assert float(_get(_get(dumped, "offer"), "unit_price")) == pytest.approx(49.0)

    text = json.dumps(dumped, default=str)
    assert "owner_statement" in text and "prod-1" in text
    assert _load(Bid, json.loads(text)) == bid

    event = LedgerEvent(
        **{
            "event_id": "ev-1",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "bid_placed",
            "auction_id": "auc-1",
            "store_id": "store-1",
            "payload": {"bid_ref": "bid-1", "nested": {"components": [1, 2, 3]}},
        }
    )
    event_dumped = _dump(event)
    assert _get(_get(event_dumped, "payload"), "bid_ref") == "bid-1"
    event_text = json.dumps(event_dumped, default=str)
    assert _load(LedgerEvent, json.loads(event_text)) == event


# ===================================================================================
# T-012 — embeddings
# ===================================================================================
@pytest.mark.epic("E1")
@pytest.mark.ticket("T-012")
def test_hash_embedding_double_is_deterministic_and_1024_dimensional():
    """C2 / C4: HashEmbedding is a deterministic 1024-dim EmbeddingProvider (D6)."""
    from services.ingest.src.embeddings import EmbeddingProvider, HashEmbedding

    # Structural conformance: HashEmbedding implements the declared interface, signature
    # for signature. Instantiates the double only; no model is loaded, no network touched.
    declared = {
        name
        for name, member in inspect.getmembers(EmbeddingProvider, callable)
        if not name.startswith("_")
    }
    assert "embed" in declared, "EmbeddingProvider must declare embed()"
    for name in sorted(declared):
        assert hasattr(HashEmbedding, name), f"HashEmbedding does not implement {name}()"
        assert inspect.signature(getattr(HashEmbedding, name)) == inspect.signature(
            getattr(EmbeddingProvider, name)
        ), f"HashEmbedding.{name}() signature diverges from EmbeddingProvider"

    text = "gentle vitamin c serum for sensitive skin"
    other = "heavy fragranced night cream for dry face"
    # The two probes are the SAME LENGTH on purpose: a provider that hashes only the
    # length of the string (or any other content-blind property) must not be able to
    # satisfy the "different text, different vector" requirement below.
    assert len(text) == len(other), (
        "the probe strings must stay the same length; otherwise a length hash passes "
        f"for an embedding ({len(text)} vs {len(other)})"
    )

    vector = list(HashEmbedding().embed(text))
    assert len(vector) == 1024, f"expected 1024 dims (D6), got {len(vector)}"
    assert all(isinstance(component, float) for component in vector)

    # Determinism across two independent instances, and across repeat calls on one.
    assert list(HashEmbedding().embed(text)) == vector
    provider = HashEmbedding()
    assert list(provider.embed(text)) == list(provider.embed(text))

    # Different text of identical length must produce a different vector: the vector has
    # to depend on the content, not on a property every string of that size shares.
    other_vector = list(HashEmbedding().embed(other))
    assert len(other_vector) == 1024, f"expected 1024 dims (D6), got {len(other_vector)}"
    assert other_vector != vector, (
        "two different texts of the same length embedded identically — this provider "
        "encodes something other than the text"
    )
    assert list(HashEmbedding().embed(other)) == other_vector, (
        "the second probe must embed deterministically too"
    )

    # A vector that is constant across its own dimensions carries no information, no
    # matter how it was derived; cosine similarity against it is meaningless (D6).
    for label, embedded in (("text", vector), ("other", other_vector)):
        assert len(set(embedded)) > 1, (
            f"the {label} vector is constant across all 1024 dimensions: "
            f"{embedded[0]!r} — that is a fill, not an embedding"
        )


# ===================================================================================
# T-014 — LLM configuration and the offline double
# ===================================================================================
@pytest.mark.epic("E1")
@pytest.mark.ticket("T-014")
def test_llm_role_model_ids_come_from_env_not_code(monkeypatch):
    """C4: per-role model ids resolve from env, with hard-coded ids confined to defaults."""
    from packages.llm import resolve_model

    roles = {
        "buyer": "BUYER_MODEL",
        "store_agent": "STORE_AGENT_MODEL",
        "interview": "INTERVIEW_MODEL",
        "extract": "EXTRACT_MODEL",
    }

    for role, env_var in roles.items():
        sentinel = f"sentinel-model-for-{role}"
        monkeypatch.setenv(env_var, sentinel)
        assert resolve_model(role) == sentinel, f"{role} did not read {env_var}"

    for role, env_var in roles.items():
        monkeypatch.delenv(env_var, raising=False)
        fallback = resolve_model(role)
        assert isinstance(fallback, str) and fallback, (
            f"{role} has no documented default when {env_var} is unset"
        )
        assert fallback != f"sentinel-model-for-{role}"

    # Model ids are config, not code: any `claude-*` literal under packages/llm must live
    # in a module-level DEFAULT* table, never inline in call sites.
    llm_root = REPO_ROOT / "packages" / "llm"
    assert llm_root.is_dir(), "packages/llm must exist"
    offenders = []
    for path in sorted(llm_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = set()
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if any("DEFAULT" in name.upper() for name in names) and getattr(node, "value", None):
                allowed.update(id(sub) for sub in ast.walk(node.value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("claude-")
                and id(node) not in allowed
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.value}")
    assert offenders == [], f"hard-coded model ids outside a defaults table: {offenders}"


@pytest.mark.epic("E1")
@pytest.mark.ticket("T-014")
def test_llm_double_answers_with_the_network_disabled(monkeypatch):
    """C9 / D3: the recorded LLM double replays deterministically with sockets disabled."""
    from packages.llm import RecordedLLM

    _block_network(monkeypatch)

    recordings = {
        "summarize the envelope": "floors respected; max discount 15%",
        "classify the intent": "cluster-serum",
    }

    double = RecordedLLM(recordings)
    first = double.complete("summarize the envelope")
    assert first == "floors respected; max discount 15%"

    # Same instance twice, and a second instance built from the same recordings.
    assert double.complete("summarize the envelope") == first
    assert RecordedLLM(recordings).complete("summarize the envelope") == first
    assert double.complete("classify the intent") == "cluster-serum"

    # An unrecorded prompt must fail loudly rather than improvise or reach the network.
    with pytest.raises(Exception) as excinfo:
        double.complete("a prompt that was never recorded")
    assert not isinstance(excinfo.value, _NetworkBlocked), (
        "the double attempted a network call for an unrecorded prompt"
    )
