"""R17 driven through ``POST /v1/bid-requests`` — the door a store agent is solicited at.

R17 has two halves and this module drives both to the place a buyer would actually feel them, the
price on a served bid:

* **A store's depth moves toward its own winners.** ``store_agent.learning.update`` folds one
  store's own outcome rows, ``to_learned_policy`` renders them as the ``learned_policy`` mapping
  the store context carries, ``ToolHooks.choose_policy_action`` reads
  ``learned_policy['actions'][cluster]['discount_pct']``, ``authorize_discount`` walls it against
  the merchant-approved envelope, and what comes out is ``offer.unit_price`` and
  ``offer.discount`` on the ``Bid`` the served route answers with. Every one of those steps is
  the product's; this module supplies the outcomes and reads the HTTP response.

* **A store never learns from a rival's discounts.** The network prior a store is *initialised*
  from is built from pitch/value-prop outcomes only. That is enforced structurally —
  ``prior.PRIOR_RECORD_FIELDS`` is an allowlist checked at import for a discount name, and
  ``prior_view`` is the only path from a record into the builder — so this module can put the
  rivals' real discount depths into the cross-store corpus and measure what the served bid does
  about them. The answer must be: nothing, byte for byte.

Why the served door and not ``store_agent.runtime.bid``
-------------------------------------------------------
``packages/store-agent/tests/test_learning.py`` already grades the loop as a library: that a
bound state drops another store's rows, that ``sample_depth`` is pure, that the prior is
byte-identical with and without discount fields. What no test had was the wire between the loop
and the agent that serves bids — the loop could have been correct and reached by nobody. So every
depth reported here is read off an HTTP response body, not off a state object.

Determinism and offline (C9)
-----------------------------
No socket leaves the process: ``TestClient`` speaks ASGI directly to ``store_agent.main
.create_app()``. No clock is read on the bid path — ``observed_at`` falls back to the runtime's
``UNKNOWN_OBSERVED_AT`` and ``expires_at`` is the request's own ``respond_by`` — so two calls on
identical inputs return byte-identical JSON. ``sample_depth`` derives its generator from
``(cluster_id, seed)`` by BLAKE2b, so the depth distribution measured over
:data:`DEPTH_SEED_WINDOW` is the same window in every process.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any

__all__ = [
    "BID_ROUTE",
    "DEPTH_SEED_WINDOW",
    "LEARNING_CLUSTER",
    "PRODUCT_REF",
    "RIVAL_STORE",
    "SUBJECT_STORE",
    "DepthShift",
    "bid_request_body",
    "mean_sampled_depth",
    "own_outcomes",
    "run_depth_shift",
    "served_discount_pct",
    "served_unit_price",
]

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The human-approved envelope this lane borrows: a 20% discount cap and a 10.00 floor on
#: ``prod-cap``, so the deepest rung of the learning grid (0.20) is exactly authorizable and
#: nothing shallower is ever refused. Borrowed rather than written here because an envelope is an
#: approved artifact — see the fixture's own ``approved_by``.
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"

#: The published operation this run bids through (``operationId: answerBidRequest``).
BID_ROUTE = "/v1/bid-requests"

#: The cluster the envelope fixture authorises bidding in. An envelope that does not pursue the
#: request's cluster declines with ``cluster_not_pursued``, which would make every measurement
#: below a 204 and the run silently vacuous.
LEARNING_CLUSTER = "cluster-warm-layers"

#: The catalogue entry both agents bid. It satisfies the request's one hard constraint
#: (``material == merino wool``) and sits far above its floor, so the only thing that can move
#: its price is the depth the agent learned.
PRODUCT_REF = "prod-cap"

#: A variant id, because a cart permalink is variant-scoped (D25) and the shipped envelope
#: fixture carries none. Kept out of the fixture: that file is an approved artifact.
VARIANT_REF = "44352913"

#: The store whose own record is shifted.
SUBJECT_STORE = "store-alpha"

#: The store with the opposite record, seeded from the *same* network prior. It is the control
#: that makes the subject's shift attributable to its own outcomes: if the prior were doing the
#: work, both would move the same way.
RIVAL_STORE = "store-rival"

#: The fixed seeds the depth *distribution* is measured over. A distribution needs more than one
#: draw and S4 asks for fixed seeds, so it is a window of them — 0..399, stated once.
DEPTH_SEED_WINDOW: tuple[int, ...] = tuple(range(400))

#: How many win/loss pairs each store's own record carries. Twenty pairs puts forty observations
#: on two rungs of a five-rung grid, which separates them without pretending a young store has a
#: thousand auctions behind it.
OWN_OUTCOME_PAIRS = 20

#: Rows per store in the cross-store pitch corpus. Small on purpose: what the prior is worth is
#: not the subject here, only what it is *blind* to.
ERA_ROWS = 12

#: The deep and shallow rungs of ``store_agent.learning.DEFAULT_DEPTH_BUCKETS`` the two stores
#: are made to converge on. Both are real rungs; a depth between them would be tallied to the
#: nearest one and the test would be asserting about the bucketing rather than the learning.
DEEP_RUNG = 0.20
SHALLOW_RUNG = 0.0


def _fixture() -> dict[str, Any]:
    return json.loads(ENVELOPE_FIXTURE.read_text(encoding="utf-8"))


def _envelope(store_id: str) -> dict[str, Any]:
    """The approved envelope, re-addressed to ``store_id`` and activated.

    ``activation`` is moved from ``shadow`` to ``active`` because R7's shadow mode is a different
    criterion with its own gate (``store_agent.modes``); an agent that is bidding is the subject
    here. Nothing else about the approved document is changed — the cap, the floors and the
    standing commitments are the merchant's.
    """
    envelope = json.loads(json.dumps(_fixture()["envelope"]))
    envelope["store_id"] = store_id
    envelope["activation"] = "active"
    for commitment in envelope["standing_commitments"]:
        commitment["provenance"]["ref"] = (
            f"envelope:{store_id}:v{envelope['version']}#{commitment['key']}"
        )
    return dict(envelope)


def store_context(
    store_id: str,
    learned_policy: Mapping[str, Any] | None,
    network_priors: Mapping[str, Any],
) -> dict[str, Any]:
    """The context the merchant service hands a hosted agent, with one store's learned policy."""
    catalog = _fixture()["catalog"][PRODUCT_REF]
    return {
        "store_id": store_id,
        "store_domain": f"{store_id}.example.com",
        "envelope": _envelope(store_id),
        "catalog": {PRODUCT_REF: {**catalog, "variant_ref": VARIANT_REF}},
        "live_state": {PRODUCT_REF: {"in_stock": True, "units_left": 7}},
        "learned_policy": None if learned_policy is None else dict(learned_policy),
        "network_priors": dict(network_priors),
    }


def bid_request_body() -> dict[str, Any]:
    """A ``BidRequest`` matching the example in the published store-agent contract.

    ``respond_by`` is a fixed instant far in the future and is what the offer's ``expires_at``
    becomes, which is half of why two runs are byte-identical.
    """
    return {
        "auction_id": "auc-learning-0001",
        "intent": {
            "intent_id": "int-learning-0001",
            "cluster_id": LEARNING_CLUSTER,
            "query": "a warm mid-layer for cold commutes",
            "category": "outerwear",
            "hard_constraints": [{"field": "material", "op": "eq", "value": "merino wool"}],
            "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
            "ship_to": "US-CA",
            "currency": "USD",
            "budget_band": "50-150",
            "created_at": "2026-01-01T00:00:00Z",
            "schema_version": "1.0.0",
        },
        "profile": {
            "pseudonym": "psn-learning-0001",
            "buckets": {
                "budget_band": "50-150",
                "category_affinity": ["outerwear"],
                "frequency_tier": "occasional",
                "region": "US-CA",
                "first_time": False,
            },
        },
        "respond_by": "2999-01-01T00:00:00Z",
    }


def network_pitch_outcomes() -> list[dict[str, Any]]:
    """The platform's cross-store corpus — and every row carries its store's real discount depth.

    Carrying them is the point. R17 permits pooling *pitch* evidence and forbids pooling discount
    elasticity, so the honest way to check the second half is to hand the builder the forbidden
    data and observe that the prior, and the bid it seeds, are unchanged by it. A corpus with the
    discounts already stripped would test the caller's tidiness, not the builder's blindness.
    """
    rows: list[dict[str, Any]] = []
    for index in range(ERA_ROWS):
        rows.append(
            {
                "cluster_id": LEARNING_CLUSTER,
                "store_id": SUBJECT_STORE,
                "value_prop": "wool_quality",
                "pitch_claims": ["merino"],
                "commitments": ["free_returns"],
                "won": index % 2 == 0,
                "discount_depth": DEEP_RUNG,
            }
        )
        rows.append(
            {
                "cluster_id": LEARNING_CLUSTER,
                "store_id": RIVAL_STORE,
                "value_prop": "fast_shipping",
                "pitch_claims": ["ships_fast"],
                "commitments": ["ships_within"],
                "won": index % 3 == 0,
                "discount_depth": SHALLOW_RUNG,
            }
        )
    return rows


def own_outcomes(store_id: str, *, deep_wins: bool, pairs: int = OWN_OUTCOME_PAIRS) -> list[dict]:
    """One store's own auction record: which depth it converted at, and which it did not.

    Each pair is one win and one loss at opposite rungs, so the record says something directional
    without changing how many auctions the store ran. ``deep_wins=False`` is the mirror image, and
    the mirror is what makes the subject's move a *direction* rather than a drift both stores
    share.
    """
    won_at = DEEP_RUNG if deep_wins else SHALLOW_RUNG
    lost_at = SHALLOW_RUNG if deep_wins else DEEP_RUNG
    commitment = "free_returns" if deep_wins else "ships_within"
    rows: list[dict[str, Any]] = []
    for _ in range(pairs):
        rows.append(
            {
                "store_id": store_id,
                "cluster_id": LEARNING_CLUSTER,
                "discount_depth": won_at,
                "won": True,
                "commitments": [commitment],
            }
        )
        rows.append(
            {
                "store_id": store_id,
                "cluster_id": LEARNING_CLUSTER,
                "discount_depth": lost_at,
                "won": False,
                "commitments": [commitment],
            }
        )
    return rows


def serve_bid(
    store_id: str,
    learned_policy: Mapping[str, Any] | None,
    network_priors: Mapping[str, Any],
) -> dict[str, Any]:
    """POST one solicitation at a freshly built store agent and return the answer.

    Returns the ``Bid`` body on a 200. A 204 — the contract's word for a decline — is returned as
    ``{"declined": <reason>}`` rather than raised, so a test can say *which* decline it got
    instead of reading a traceback; a run in which the agent declines is a run that measured
    nothing, and it should fail on an assertion that names the reason.
    """
    from fastapi.testclient import TestClient
    from store_agent.main import create_app
    from store_agent.solicitation import DECLINE_REASON_HEADER, configure_solicitation

    app = create_app()
    configure_solicitation(app, context=store_context(store_id, learned_policy, network_priors))
    with TestClient(app) as client:
        response = client.post(BID_ROUTE, json=bid_request_body())
    if response.status_code != 200:
        return {
            "declined": response.headers.get(DECLINE_REASON_HEADER, ""),
            "status_code": response.status_code,
        }
    body: dict[str, Any] = response.json()
    body["status_code"] = response.status_code
    return body


def served_discount_pct(bid: Mapping[str, Any]) -> float:
    """The discount depth, as a PERCENT, on a served bid. 0.0 when the offer carries none.

    An offer with no ``discount`` is a bid at list price, which is a real answer and the one a
    cold agent gives — distinct from a decline, which :func:`serve_bid` reports separately.
    """
    offer = bid.get("offer")
    if not isinstance(offer, Mapping):
        raise AssertionError(f"no offer on this answer: {bid!r}")
    discount = offer.get("discount")
    if discount is None:
        return 0.0
    return float(discount["value"])


def served_unit_price(bid: Mapping[str, Any]) -> float:
    """The price on a served bid — the number a buyer would be shown."""
    offer = bid.get("offer")
    if not isinstance(offer, Mapping):
        raise AssertionError(f"no offer on this answer: {bid!r}")
    return float(offer["unit_price"])


def mean_sampled_depth(state: Any, seeds: Iterable[int] = DEPTH_SEED_WINDOW) -> float:
    """The mean depth this state's sampler draws over a fixed window of seeds.

    ``sample_depth`` is the *distribution* R17 speaks of; ``best_depth`` (what the served policy
    renders) is its mode. Both are reported because "the depth distribution shifts" is a claim
    about the whole sampler, and a loop that moved only its argmax would satisfy the served-bid
    assertion while still exploring exactly as it did before.
    """
    from store_agent.learning import sample_depth

    return float(fmean(float(sample_depth(state, LEARNING_CLUSTER, seed)) for seed in seeds))


@dataclass(frozen=True)
class DepthShift:
    """Everything one R17 run produced. Every ``*_bid`` is an HTTP response body."""

    #: The subject before it had run anything: a state seeded from the network prior and nothing
    #: else. R17's "initialised from network priors" arm — the depth grid starts flat however much
    #: the network knows.
    cold_bid: dict[str, Any]
    #: The subject after its own record: it converted deep and lost shallow.
    learned_bid: dict[str, Any]
    #: The same request served twice from the same learned state, for the determinism assertion.
    learned_bid_again: dict[str, Any]
    #: The rival, seeded from the SAME prior object, after the mirror-image record.
    rival_bid: dict[str, Any]
    #: The subject after the rival's own outcome rows were folded into its state. A bound state
    #: drops foreign rows, so this must be the learned bid byte for byte.
    polluted_bid: dict[str, Any]
    #: The cold subject when those discount fields are absent from the corpus entirely. Compared
    #: against :attr:`cold_bid`, which was built from a corpus that DOES carry them.
    cold_bid_without_rival_discounts: dict[str, Any]
    #: The cold subject when every rival in the corpus is recorded discounting to 80%.
    cold_bid_with_deep_rival_discounts: dict[str, Any]
    #: ``{label: mean sampled depth}`` over :data:`DEPTH_SEED_WINDOW`, as fractions.
    mean_depths: dict[str, float]
    #: The same window sampled twice, for the reproducibility assertion.
    mean_depths_again: dict[str, float]
    #: The rendered ``learned_policy`` mappings, so a test can show the served price and the
    #: policy behind it are the same decision.
    policies: dict[str, dict[str, Any]]
    #: The three network priors the run built, compared for equality by the tests.
    priors: dict[str, Any]

    def discount_pct(self, which: str) -> float:
        return served_discount_pct(getattr(self, which))


def run_depth_shift() -> DepthShift:
    """Build the two stores' policies from outcomes and serve every bid they imply."""
    from store_agent.learning import (
        build_network_prior,
        initial_state,
        to_context_priors,
        to_learned_policy,
        update,
    )

    corpus = network_pitch_outcomes()
    prior = build_network_prior(corpus)
    priors_view = to_context_priors(prior)

    # The same corpus with every discount field removed, and one in which the rivals are recorded
    # discounting to 80%. Both must produce the same prior and the same served bid as `corpus`.
    stripped = [
        {key: value for key, value in row.items() if "discount" not in key} for row in corpus
    ]
    deepened = [
        dict(row, discount_depth=0.8) if row["store_id"] == RIVAL_STORE else dict(row)
        for row in corpus
    ]
    prior_stripped = build_network_prior(stripped)
    prior_deepened = build_network_prior(deepened)

    # Two stores, one prior object, deliberately shared: it is frozen all the way down, so
    # sharing it cannot become a channel between them.
    cold_subject = initial_state(prior, store_id=SUBJECT_STORE)
    cold_rival = initial_state(prior, store_id=RIVAL_STORE)
    learned_subject = update(cold_subject, own_outcomes(SUBJECT_STORE, deep_wins=True))
    learned_rival = update(cold_rival, own_outcomes(RIVAL_STORE, deep_wins=False))
    # The rival's own rows, offered to the subject's bound state. R17's privacy clause.
    polluted_subject = update(learned_subject, own_outcomes(RIVAL_STORE, deep_wins=False))

    policies = {
        "cold": to_learned_policy(cold_subject),
        "learned": to_learned_policy(learned_subject),
        "rival": to_learned_policy(learned_rival),
        "polluted": to_learned_policy(polluted_subject),
    }

    def _mean_window() -> dict[str, float]:
        return {
            "cold": mean_sampled_depth(cold_subject),
            "learned": mean_sampled_depth(learned_subject),
            "rival": mean_sampled_depth(learned_rival),
        }

    return DepthShift(
        cold_bid=serve_bid(SUBJECT_STORE, policies["cold"], priors_view),
        learned_bid=serve_bid(SUBJECT_STORE, policies["learned"], priors_view),
        learned_bid_again=serve_bid(SUBJECT_STORE, policies["learned"], priors_view),
        rival_bid=serve_bid(RIVAL_STORE, policies["rival"], priors_view),
        polluted_bid=serve_bid(SUBJECT_STORE, policies["polluted"], priors_view),
        cold_bid_without_rival_discounts=serve_bid(
            SUBJECT_STORE,
            to_learned_policy(initial_state(prior_stripped, store_id=SUBJECT_STORE)),
            to_context_priors(prior_stripped),
        ),
        cold_bid_with_deep_rival_discounts=serve_bid(
            SUBJECT_STORE,
            to_learned_policy(initial_state(prior_deepened, store_id=SUBJECT_STORE)),
            to_context_priors(prior_deepened),
        ),
        mean_depths=_mean_window(),
        mean_depths_again=_mean_window(),
        policies=policies,
        priors={
            "as_recorded": prior,
            "discounts_stripped": prior_stripped,
            "rivals_deepened": prior_deepened,
        },
    )


def declined_reason(bid: Mapping[str, Any]) -> str | None:
    """The decline reason on an answer that was not a bid, else ``None``."""
    reason = bid.get("declined")
    return None if reason is None else str(reason)


def canonical(bid: Mapping[str, Any]) -> str:
    """A served answer as a stable string, for byte-for-byte comparison of two of them."""
    return json.dumps(bid, sort_keys=True, separators=(",", ":"))


def store_ids() -> Sequence[str]:
    """The two stores this lane serves, in a fixed order."""
    return (SUBJECT_STORE, RIVAL_STORE)
