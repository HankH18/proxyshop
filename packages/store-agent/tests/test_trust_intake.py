"""The served trust-intake door: what it takes in, what it refuses, and what it remembers.

``AgentRunner.ingest_trust_event`` had eighteen callers in this package and every one of them
was a test. This file grades the transport that finally gives it a production one, and it
grades it over the served route rather than by calling the runner — the whole class of defect
here is code that works when a test calls it and is unreachable when a request arrives.

Every payload driven through the door is built by ``trust.feedback.trust_event_payload``, the
real emitter on the other side of the seam, rather than by a hand-written dict. That is the
lesson of T-259: for sixty of sixty events, both suites were green while the emitter produced
a shape the intake refused, because each side only ever met its own double.

The state assertions read ``agent_runner(app).trust_posture`` back off the application AFTER
the response, never off the response body. A door that computed a posture, answered with it
and dropped it would satisfy every assertion on the body and leave the process exactly as
unmoved as it was before this ticket.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from store_agent.main import create_app
from store_agent.solicitation import CONTEXT_ENV, configure_solicitation, load_context_from_env
from store_agent.trust_intake import agent_runner, configure_trust_intake
from store_agent.trust_intake.routes import MISROUTED_STATUS, UNCONFIGURED_STATUS
from trust.feedback import TRUST_EVENT_PATH, trust_event_payload

REPO_ROOT = Path(__file__).resolve().parents[3]

ENVELOPES = {
    "store-alpha": REPO_ROOT / "fixtures/envelopes/store-alpha.approved.json",
    "store-beta": REPO_ROOT / "fixtures/envelopes/store-beta.approved.json",
}


def _context(store_id: str) -> dict[str, Any]:
    """One of this repo's own approved envelopes, loaded exactly as the container loads it."""
    context = load_context_from_env({CONTEXT_ENV: str(ENVELOPES[store_id])})
    assert context is not None and context["store_id"] == store_id
    return context


def _app(store_id: str | None) -> Any:
    app = create_app()
    configure_solicitation(app, context=None if store_id is None else _context(store_id))
    return app


def _emitted(
    store_id: str,
    *,
    dim: str = "feedback_match",
    delta: float = -0.25,
    event_id: str = "ev-intake-1",
    reason_code: str = "contradicted",
) -> dict[str, Any]:
    """A payload built by the REAL emitter, so this file cannot drift from the sender."""
    return trust_event_payload(
        {
            "store_id": store_id,
            "dim": dim,
            "delta": delta,
            "reason_code": reason_code,
            "event": {
                "event_id": event_id,
                "ts": "2026-01-01T00:00:00Z",
                "kind": "feedback",
                "store_id": store_id,
                "order_ref": "ord-0001",
                "payload": {"matched_pitch": False, "buyer_email": "dana@example.com"},
            },
        }
    )


# =============================================================================================
# Arming
# =============================================================================================


def test_the_emitter_this_file_drives_produces_a_payload_with_a_real_delta() -> None:
    """If the generator degenerated, every acceptance assertion below would grade a constant."""
    payload = _emitted("store-alpha")
    assert payload["store_id"] == "store-alpha"
    assert payload["dim"] == "feedback_match"
    assert payload["delta"] == pytest.approx(-0.25)
    assert payload["event"]["event_id"] == "ev-intake-1"
    # And the scrub really ran, so an accepted payload is proof the door takes a SCRUBBED
    # event rather than proof the test forgot to plant anything.
    assert "dana@example.com" not in json.dumps(payload)


# =============================================================================================
# Acceptance
# =============================================================================================


def test_a_pushed_event_moves_the_posture_of_the_served_process() -> None:
    app = _app("store-alpha")
    with TestClient(app) as client:
        response = client.post(TRUST_EVENT_PATH, json=_emitted("store-alpha"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["store_id"] == "store-alpha"
    assert body["dim"] == "feedback_match"
    assert body["delta"] == pytest.approx(-0.25)
    assert body["event_id"] == "ev-intake-1"
    assert body["posture"]["stance"] == "guarded"

    posture = agent_runner(app).trust_posture
    assert [(str(s.dim), s.net_delta, s.observations) for s in posture.signals] == [
        ("feedback_match", pytest.approx(-0.25), 1)
    ]


def test_two_pushes_accumulate_in_one_process() -> None:
    """The runner is the process's, not the request's — the point of caching it on app.state."""
    app = _app("store-alpha")
    with TestClient(app) as client:
        client.post(TRUST_EVENT_PATH, json=_emitted("store-alpha", event_id="ev-a", delta=-0.25))
        second = client.post(
            TRUST_EVENT_PATH,
            json=_emitted("store-alpha", event_id="ev-b", delta=-0.1, dim="shipped_on_time"),
        )

    assert second.status_code == 200, second.text
    posture = agent_runner(app).trust_posture
    assert [str(s.dim) for s in posture.signals] == ["feedback_match", "shipped_on_time"]
    assert sum(s.net_delta for s in posture.signals) == pytest.approx(-0.35)


def test_a_positive_delta_reinforces_rather_than_guards() -> None:
    """Honest traffic: a store that did what it promised is not penalised by this door."""
    app = _app("store-beta")
    with TestClient(app) as client:
        response = client.post(
            TRUST_EVENT_PATH,
            json=_emitted("store-beta", delta=0.2, reason_code="verified", dim="price_honored"),
        )

    assert response.status_code == 200, response.text
    assert response.json()["posture"]["stance"] == "reinforced"
    assert agent_runner(app).trust_posture.weak_dimensions == ()


@pytest.mark.parametrize("store_id", sorted(ENVELOPES))
@pytest.mark.parametrize(
    "dim",
    [
        "price_honored",
        "discount_honored",
        "shipped_on_time",
        "not_returned",
        "feedback_match",
        "catalog_claim_accuracy",
    ],
)
def test_every_dimension_of_every_shipped_envelope_is_accepted(store_id: str, dim: str) -> None:
    """The honest-traffic corpus: both approved fixtures, all six published dimensions.

    A refusal that closes a hostile case and starts refusing legitimate input has shipped in
    this repo before, so the door is driven across the whole product of what it will really
    see rather than across the one case the acceptance test above happens to use.
    """
    app = _app(store_id)
    with TestClient(app) as client:
        response = client.post(
            TRUST_EVENT_PATH, json=_emitted(store_id, dim=dim, delta=-0.05, event_id=f"ev-{dim}")
        )
    assert response.status_code == 200, response.text
    assert response.json()["dim"] == dim


# =============================================================================================
# Refusals
# =============================================================================================


def test_an_event_naming_another_store_is_refused_and_absorbed_by_nobody() -> None:
    """R13's privacy property, at the receiving end: a neighbour's evidence is never mine."""
    app = _app("store-beta")
    with TestClient(app) as client:
        response = client.post(TRUST_EVENT_PATH, json=_emitted("store-alpha"))

    assert response.status_code == MISROUTED_STATUS, response.text
    assert response.json()["detail"]["error"] == "misrouted_trust_event"
    assert agent_runner(app).trust_posture.signals == (), (
        "a misrouted event moved this store's posture; sealed state is per store and "
        "absorbing a neighbour's feedback corrupts the posture of both undetectably"
    )


def test_a_non_finite_delta_is_refused_rather_than_blinding_a_dimension() -> None:
    """One `nan` poisons a dimension permanently: the sum stays `nan` and reads `neutral`."""
    payload = _emitted("store-alpha")
    payload["delta"] = math.inf
    app = _app("store-alpha")
    with TestClient(app) as client:
        response = client.post(TRUST_EVENT_PATH, content=json.dumps(payload).encode())

    assert response.status_code == 422, response.text
    assert agent_runner(app).trust_posture.signals == ()


def test_a_body_that_is_not_a_trust_event_payload_is_refused_before_the_runner() -> None:
    app = _app("store-alpha")
    with TestClient(app) as client:
        response = client.post(TRUST_EVENT_PATH, json={"store_id": "store-alpha"})

    assert response.status_code == 422, response.text
    assert agent_runner(app).trust_posture.signals == ()


def test_an_unconfigured_process_refuses_and_says_which_condition_it_is_in() -> None:
    """Fail closed, and distinguishably: 503 is not 200 and it is not a silent 204 either."""
    app = _app(None)
    with TestClient(app) as client:
        response = client.post(TRUST_EVENT_PATH, json=_emitted("store-alpha"))

    assert response.status_code == UNCONFIGURED_STATUS, response.text
    assert response.json()["detail"]["error"] == "store_context_unconfigured"
    assert agent_runner(app) is None


# =============================================================================================
# The runner itself
# =============================================================================================


def test_the_runner_is_resolved_once_and_shared_by_every_request() -> None:
    app = _app("store-alpha")
    assert agent_runner(app) is agent_runner(app)
    with TestClient(app) as client:
        client.post(TRUST_EVENT_PATH, json=_emitted("store-alpha"))
        assert agent_runner(app).trust_posture.signals != ()


def test_an_explicitly_wired_runner_is_used_instead_of_one_built_from_the_context() -> None:
    """The composition-root path: a process that already holds its runner hands it over."""
    app = _app("store-alpha")
    wired = agent_runner(_app("store-alpha"))
    configure_trust_intake(app, runner=wired)
    with TestClient(app) as client:
        assert client.post(TRUST_EVENT_PATH, json=_emitted("store-alpha")).status_code == 200
    assert wired.trust_posture.signals != ()


def test_the_runner_never_submits_and_therefore_never_looks_live() -> None:
    """store-alpha's approved envelope says `shadow`, so its runner submits nothing.

    The reason changed and the property did not. This used to hold because the process had no
    submitter at all and was therefore built `shadow` whatever the envelope said — which is
    also why R7 was unobservable from outside the process. The runner is now built in the mode
    its envelope states and its submitter is the bid door's response channel, and
    ``fixtures/envelopes/store-alpha.approved.json`` states ``"activation": "shadow"``. So the
    same two assertions hold, and they now mean what they say: this store has not been
    activated. ``store-beta``'s shipped envelope says `active`, and the case below is its
    control.
    """
    runner = agent_runner(_app("store-alpha"))
    assert runner.submits is False
    assert str(runner.mode) in {"EnvelopeActivation.shadow", "shadow"}


def test_a_shipped_envelope_that_states_active_builds_a_submitting_runner() -> None:
    """The control for the case above: `shadow` is read off the envelope, not hard-coded.

    Without this, a runner pinned to `shadow` for any reason at all would satisfy the
    assertion above, and R7's activation half would be ungraded in this file.
    """
    runner = agent_runner(_app("store-beta"))
    assert runner.submits is True
    assert str(runner.mode) in {"EnvelopeActivation.active", "active"}


def test_the_bid_door_still_answers_after_the_trust_door_was_mounted() -> None:
    """Honest traffic, on the route this package already served. Both routers, one app."""
    app = _app("store-alpha")
    assert sorted(app.state.mounted_routers) == [
        "store_agent.solicitation.routes",
        "store_agent.trust_intake.routes",
    ]
    assert sorted(app.openapi()["paths"]) == ["/v1/bid-requests", TRUST_EVENT_PATH]
