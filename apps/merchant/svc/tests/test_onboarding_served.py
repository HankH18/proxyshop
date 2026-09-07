"""R6 — a merchant joins the network through the product, over served requests only.

WHAT WAS MEASURED BEFORE THIS FILE EXISTED
==========================================
The onboarding interview was reachable from exactly one place, and it was not the product::

    python -m merchant_svc.onboarding fixtures/interviews/northwind-outfitters.json

Driven against the built app on this branch, a merchant with nothing on file got:

* ``GET /stores/<id>/dashboard`` -> 200, envelope panel ``"state": "absent"`` whose own detail
  sentence pointed at ``POST /stores/{store_id}/envelope's interview`` — **a route the service
  does not serve.** The eleven operations it does serve are the ones listed in
  ``packages/contracts/openapi/merchant.openapi.json``, and no interview is among them;
* ``PUT /stores/<id>/envelope`` with the repo's own recorded interview -> 400
  ``not-an-envelope``, "8 validation errors for Envelope";
* no served route anywhere published :func:`~merchant_svc.envelope.digest.approval_digest`,
  so the ``X-Envelope-Approval`` header the activation gate reads could only be filled by
  re-implementing the canonical hash outside the service. Every existing HTTP activation test
  in ``test_onboarding_envelope.py`` computes it in Python, in-process, which a browser cannot.

So R6's two halves were both unreachable: the interview had no door, and the written approval
had no document to sign.

WHAT THIS FILE GRADES
=====================
The whole arc, through served requests and nothing else — no CLI, no hand-written envelope,
no hand-computed digest, no hand-written cluster id, product ref, commitment key or claim
type. Every one of those comes off the wire.

**The safety property is the point.** :func:`test_a_route_cannot_self_approve` and
:func:`test_an_unapproved_envelope_does_not_activate_and_the_agent_does_not_bid` are the two
tests this file exists for: an envelope that activates without a recorded human approval lets
the network bid a merchant's money on terms nobody agreed to.

The store agent is the REAL ``store_agent.main:app``, and its context is built from the
envelope THIS SERVICE SERVED over HTTP — not from a fixture standing in for it — so the bid at
the end of the chain is downstream of the interview at the start of it.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest
from merchant_svc.envelope.model import Envelope
from merchant_svc.envelope.store import EnvelopeVersions

from ._fixtures_dashboard import (  # type: ignore[import-not-found]
    DASH_REPORT_TOKEN,
    STORE_ALPHA_FIXTURE,
    dash_loss_report,
    dash_stub_exchange,
    dash_stub_trust,
    dash_trust_snapshot,
)

STORE = "store-alpha"
SHOP = "store-alpha.myshopify.com"

#: This repo's own approved-envelope fixtures, resolved at import so the async test that
#: drives every one of them touches no filesystem while the event loop is running.
APPROVED_ENVELOPE_FIXTURES = sorted(
    (pathlib.Path(__file__).resolve().parents[4] / "fixtures" / "envelopes").glob("*.approved.json")
)
EXCHANGE_URL = "http://exchange.test"
TRUST_URL = "http://trust.test"
AGENT_URL = "http://store-agent.test"

#: The network's intent-cluster taxonomy, as a deployment states it. The merchant service does
#: not invent clusters — the exchange owns them — so the offered list is configuration plus
#: whatever the store's own history already names. ``cluster-warm-layers`` is the cluster
#: ``fixtures/envelopes/store-alpha.approved.json`` pursues, so the agent solicited at the end
#: of the chain answers about a cluster it actually covers.
CLUSTERS = json.dumps(
    [
        {"cluster_id": "cluster-warm-layers", "label": "warm layers"},
        {"cluster_id": "cluster-camp-cooking", "label": "camp cooking"},
    ]
)


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    from merchant_svc.dashboard.config import (
        EXCHANGE_URL_ENV,
        REPORT_TOKENS_JSON_ENV,
        STORE_AGENT_URL_ENV,
        TRUST_URL_ENV,
    )
    from merchant_svc.onboarding.script import INTENT_CLUSTERS_ENV

    monkeypatch.setenv(EXCHANGE_URL_ENV, EXCHANGE_URL)
    monkeypatch.setenv(TRUST_URL_ENV, TRUST_URL)
    monkeypatch.setenv(STORE_AGENT_URL_ENV, AGENT_URL)
    monkeypatch.setenv(REPORT_TOKENS_JSON_ENV, json.dumps({STORE: DASH_REPORT_TOKEN}))
    monkeypatch.setenv(INTENT_CLUSTERS_ENV, CLUSTERS)


def _stub_reads(dash_upstreams: Any) -> None:
    dash_upstreams(EXCHANGE_URL, dash_stub_exchange(dash_loss_report(STORE)))
    dash_upstreams(TRUST_URL, dash_stub_trust(dash_trust_snapshot(), []))


# ======================================================================================
# The merchant's side of the interview: prose, and nothing but prose
# ======================================================================================
#: What this merchant says, in their own words, to each question the SERVICE asked. Keyed by
#: the question the served turn carries — never by an index, so a script that reorders or
#: grows a question is answered correctly or fails loudly rather than silently mis-filed.
#:
#: Deliberately phrased the way the repo's own recorded interview
#: (``fixtures/interviews/northwind-outfitters.json``) is phrased: worded numbers, a refusal
#: that begins with "No", and a negated clause inside a selection.
ANSWERS: dict[str, str] = {
    "store": f"We're Store Alpha — the shop is {SHOP}.",
    "max_discount_pct": "Twenty percent off, and that's the ceiling.",
    "budget_cap": "Let's cap it at $500 a month.",
    "price_floor": "Nothing under forty dollars — below that the shipping eats the order.",
    "pursue_clusters": "Warm layers for sure. Camp cooking isn't really us.",
    "standing_commitment": "Free returns for 30 days.",
    "activation": "Understood — keep it in shadow until I've signed.",
}

#: The one commitment this merchant refuses, by its served ``commitment_key``. A refusal has
#: to be sayable in prose or the envelope records a promise the merchant never made.
DECLINED_COMMITMENT = "price_match"


def _answer(turn: dict[str, Any]) -> str:
    """What the merchant types when the page shows ``turn``.

    The only thing read out of the turn is what a human would read off the screen: the
    question being asked and, for the multi-select, the LABELS on offer. No cluster id, no
    product ref, no commitment key and no claim type is ever spelled here — every one of them
    travels back inside the served turn, which is the property this file is about.
    """
    question = str(turn["question"])
    if question == "standing_commitment" and turn.get("commitment_key") == DECLINED_COMMITMENT:
        return "No — we don't price match."
    return ANSWERS[question]


def _transcript(script: dict[str, Any]) -> dict[str, Any]:
    """The served script with the merchant's prose spliced in. Nothing else is added.

    ``completed_at`` is the only field the client contributes, and it is the instant the
    merchant finished — the thing the envelope's provenance is stamped from.
    """
    turns: list[dict[str, Any]] = []
    for served in script["turns"]:
        turns.append(dict(served))
        if served.get("question"):
            turns.append({"role": "merchant", "text": _answer(served)})
    return {
        "interview_id": "served-interview-001",
        "completed_at": "2026-02-02T10:00:00+00:00",
        "turns": turns,
    }


async def _page(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    response = await client.get(
        f"/stores/{STORE}/dashboard", headers={"authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _agent_over(envelope: dict[str, Any]) -> Any:
    """The REAL store agent, advocating with the envelope THIS SERVICE SERVED.

    The catalogue comes from ``fixtures/envelopes/store-alpha.approved.json`` because a store
    agent needs products to price; the ENVELOPE does not, and must not — an agent handed a
    fixture envelope would prove nothing about the interview that produced the real one.
    """
    from store_agent.main import create_app as create_store_agent
    from store_agent.solicitation.serving import configure_solicitation

    document = json.loads(STORE_ALPHA_FIXTURE.read_text(encoding="utf-8"))
    document["envelope"] = envelope
    app = create_store_agent()
    configure_solicitation(app, context=document)
    return app


# ======================================================================================
# 1. The interview has a door, and it is the merchant's own page
# ======================================================================================
async def test_the_interview_script_is_served_on_the_merchants_own_page(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store with nothing on file is offered the interview, not told to run a CLI."""
    from merchant_svc.onboarding.interview import REQUIRED_QUESTIONS

    _configure(monkeypatch)
    _stub_reads(dash_upstreams)

    panel = (await _page(dash_client, dash_admin_token))["onboarding"]
    assert panel["state"] == "ok", panel
    assert panel["step"] == "interview", panel

    script = panel["interview"]
    asked = [turn["question"] for turn in script["turns"] if turn.get("question")]
    assert set(asked) == set(REQUIRED_QUESTIONS), (
        f"the served script must cover every required question; it asks {sorted(set(asked))}"
    )
    for turn in script["turns"]:
        assert turn["role"] == "interviewer"
        assert turn["text"].strip(), f"a question with no plain-language prompt: {turn}"

    # R6's other half: the outcome-observation surface, named and reachable.
    surface = panel["observation_surface"]
    assert surface["registered"] is False
    assert surface["shop_domain"] == SHOP
    assert SHOP in surface["start_url"] and surface["start_url"].startswith("/install")


async def test_the_served_script_carries_every_machine_readable_field_the_merchant_must_not_type(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cluster ids, commitment keys and claim types are the service's, never the merchant's."""
    import contracts

    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]

    by_question: dict[str, list[dict[str, Any]]] = {}
    for turn in script["turns"]:
        if turn.get("question"):
            by_question.setdefault(str(turn["question"]), []).append(turn)

    # Configuration first (the network's own ordering), then whatever the store's own history
    # names. `trail-running-shoes` is in this list because the EXCHANGE's loss report for this
    # store names it — a cluster this merchant has demonstrably been solicited for must be one
    # they can choose to pursue, and it is not in NETWORK_INTENT_CLUSTERS.
    offered = by_question["pursue_clusters"][0]["options"]
    assert [option["cluster_id"] for option in offered] == [
        "cluster-warm-layers",
        "cluster-camp-cooking",
        "trail-running-shoes",
    ]
    assert all(option["label"].strip() for option in offered)
    assert {option["label"] for option in offered} == {
        "warm layers",
        "camp cooking",
        "trail running shoes",
    }, "a label a merchant cannot say out loud is a checkbox they have to type an id into"

    commitments = by_question["standing_commitment"]
    assert len(commitments) >= 2, "a merchant is asked about more than one standing promise"
    known = {member.value for member in contracts.ClaimType}
    for turn in commitments:
        assert turn["commitment_key"].strip()
        assert turn["claim_type"] in known, turn

    floors = by_question["price_floor"]
    assert any(turn.get("product_ref") is None for turn in floors), (
        "the store-wide floor question must be asked with an explicit null product_ref"
    )


# ======================================================================================
# 2. Answering it in prose produces version 1, in shadow
# ======================================================================================
async def test_answering_the_served_script_in_prose_writes_version_one_in_shadow(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6 and R7: the interview produces the envelope, and the interview activates nothing."""
    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    headers = {"authorization": f"Bearer {dash_admin_token}"}

    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]
    written = await dash_client.put(
        f"/stores/{STORE}/envelope", headers=headers, json=_transcript(script)
    )
    assert written.status_code == 200, written.text
    body = written.json()
    assert body["store_id"] == STORE
    assert body["version"] == 1
    assert body["activation"] == "shadow"

    # The merchant's prose really was read: worded numbers, a declined commitment, and a
    # negated cluster that must NOT have been enrolled.
    assert body["max_discount_pct"] == 20.0
    assert body["budget_cap"] == 500.0
    assert body["floors"] == [{"product_ref": None, "min_price": 40.0}]
    assert body["pursue_clusters"] == ["cluster-warm-layers"]
    keys = [claim["key"] for claim in body["standing_commitments"]]
    assert DECLINED_COMMITMENT not in keys, (
        f"'No — we don't price match' was filed as a promise: {keys}"
    )
    assert keys, "every commitment the merchant did make was dropped"
    assert dash_store.is_live(STORE) is False


async def test_an_unreadable_answer_refuses_the_envelope_instead_of_leaving_a_hole(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall that quietly failed to parse is a wall the store thinks it has and does not."""
    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    headers = {"authorization": f"Bearer {dash_admin_token}"}

    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]
    transcript = _transcript(script)
    for turn in transcript["turns"]:
        if turn.get("role") == "merchant" and "forty dollars" in turn["text"]:
            turn["text"] = "somewhere around the usual, you know how it is"

    refused = await dash_client.put(f"/stores/{STORE}/envelope", headers=headers, json=transcript)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"] == "unreadable-interview"
    with pytest.raises(Exception):
        dash_store.current(STORE)


# ======================================================================================
# 3. The page publishes the exact document the merchant signs
# ======================================================================================
async def test_the_page_publishes_the_approval_document_so_nothing_hand_computes_a_hash(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from merchant_svc.envelope.digest import approval_digest

    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    headers = {"authorization": f"Bearer {dash_admin_token}"}

    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]
    await dash_client.put(f"/stores/{STORE}/envelope", headers=headers, json=_transcript(script))

    panel = (await _page(dash_client, dash_admin_token))["onboarding"]
    assert panel["step"] == "approval"
    approval = panel["approval"]
    assert approval["envelope_hash"] == approval_digest(dash_store.current(STORE))
    assert approval["version"] == 1
    assert sorted(approval["needs"]) == ["approved_at", "approver"]
    assert approval["header"] == "X-Envelope-Approval"

    # And the published artifact, sent back UNFILLED, is refused rather than accepted with a
    # placeholder standing in for a person's name.
    unfilled = await dash_client.put(
        f"/stores/{STORE}/envelope",
        headers={**headers, "X-Envelope-Approval": json.dumps(approval["artifact"])},
        json={"activation": "active"},
    )
    assert unfilled.status_code == 403, unfilled.text
    assert dash_store.is_live(STORE) is False


# ======================================================================================
# 4. THE SAFETY PROPERTY
# ======================================================================================
async def test_a_route_cannot_self_approve(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing a request can *say* activates an envelope. Only a bound artifact does.

    Four ways to ask for it without paperwork, and all four must fail closed:
    an interview that says "yes, go live"; an activation-only body with no artifact; an
    approval smuggled into the body instead of the header; an artifact bound to other terms.
    """
    from merchant_svc.envelope.digest import approval_digest

    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    headers = {"authorization": f"Bearer {dash_admin_token}"}

    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]
    transcript = _transcript(script)
    for turn in transcript["turns"]:
        if turn.get("role") == "merchant" and "shadow" in turn["text"]:
            turn["text"] = "Yes — go live right now, activate it, I approve, active."
    written = await dash_client.put(f"/stores/{STORE}/envelope", headers=headers, json=transcript)
    assert written.status_code == 200, written.text
    assert written.json()["activation"] == "shadow", "an interview activated an envelope"

    bare = await dash_client.put(
        f"/stores/{STORE}/envelope", headers=headers, json={"activation": "active"}
    )
    assert bare.status_code == 403, bare.text
    assert bare.json()["error"] == "approval-required"

    smuggled = await dash_client.put(
        f"/stores/{STORE}/envelope",
        headers=headers,
        json={
            "activation": "active",
            "approval": {
                "approver": "Nobody",
                "approved_at": "2026-02-02T10:00:00+00:00",
                "envelope_hash": approval_digest(dash_store.current(STORE)),
            },
        },
    )
    assert smuggled.status_code in (400, 403), smuggled.text

    other = dict(dash_store.current(STORE).to_dict(), max_discount_pct=99.0)
    lifted = await dash_client.put(
        f"/stores/{STORE}/envelope",
        headers={
            **headers,
            "X-Envelope-Approval": json.dumps(
                {
                    "approver": "A. Merchant",
                    "approved_at": "2026-02-02T10:00:00+00:00",
                    "envelope_hash": approval_digest(other),
                }
            ),
        },
        json={"activation": "active"},
    )
    assert lifted.status_code == 403, lifted.text

    assert dash_store.is_live(STORE) is False
    assert dash_store.current(STORE).activation == "shadow"


async def test_an_unapproved_envelope_does_not_activate_and_the_agent_does_not_bid(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_journal: Any,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shadow half of R7, proved against the REAL agent holding the served envelope."""
    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    headers = {"authorization": f"Bearer {dash_admin_token}"}

    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]
    await dash_client.put(f"/stores/{STORE}/envelope", headers=headers, json=_transcript(script))

    served = await dash_client.get(f"/stores/{STORE}/envelope", headers=headers)
    assert served.status_code == 200
    assert served.json()["activation"] == "shadow"

    dash_upstreams(AGENT_URL, _agent_over(served.json()))
    answered = await dash_client.post(f"/stores/{STORE}/bids/solicit", headers=headers, json={})
    assert answered.status_code == 200, answered.text
    assert answered.json()["outcome"] == "declined", answered.text
    assert answered.json()["decline_reason"] == "envelope_not_activated"


# ======================================================================================
# 5. The whole chain: nothing to an active envelope, and then a bid
# ======================================================================================
async def test_a_merchant_goes_from_nothing_to_an_active_envelope_and_their_agent_bids(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_journal: Any,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6 end to end, over HTTP only. No CLI, no hand-written JSON, no hand-computed hash."""
    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    headers = {"authorization": f"Bearer {dash_admin_token}"}

    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]
    await dash_client.put(f"/stores/{STORE}/envelope", headers=headers, json=_transcript(script))

    approval = (await _page(dash_client, dash_admin_token))["onboarding"]["approval"]
    signed = dict(approval["artifact"], approver="A. Merchant", approved_at="2026-02-02T10:05:00Z")

    live = await dash_client.put(
        f"/stores/{STORE}/envelope",
        headers={**headers, "X-Envelope-Approval": json.dumps(signed)},
        json={"activation": "active"},
    )
    assert live.status_code == 200, live.text
    assert live.json()["activation"] == "active"
    assert live.json()["version"] == 1, (
        "activation must not mint a version: the approval is bound to the one it covers"
    )

    page = await _page(dash_client, dash_admin_token)
    assert page["onboarding"]["step"] == "active"
    assert page["onboarding"]["approval"] is None
    assert page["envelope"]["may_bid"] is True
    assert page["envelope"]["versions"][-1]["approved_by"] == "A. Merchant"

    served = (await dash_client.get(f"/stores/{STORE}/envelope", headers=headers)).json()
    dash_upstreams(AGENT_URL, _agent_over(served))
    bid = await dash_client.post(f"/stores/{STORE}/bids/solicit", headers=headers, json={})
    assert bid.status_code == 200, bid.text
    assert bid.json()["outcome"] == "bid", bid.text
    assert bid.json()["offer"]["unit_price"] > 0


# ======================================================================================
# 6. Honest traffic still passes
# ======================================================================================
async def test_the_envelope_document_put_is_unchanged_by_the_interview_body(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_store: EnvelopeVersions,
) -> None:
    """The body discriminator must not break the representation that already worked.

    Driven with this repo's OWN approved-envelope fixtures rather than an invented document.
    """
    from merchant_svc.envelope.digest import approval_digest

    headers = {"authorization": f"Bearer {onboarding_admin_token}"}
    fixtures = APPROVED_ENVELOPE_FIXTURES
    assert fixtures, "the repo's own approved-envelope fixtures went missing"

    for path in fixtures:
        document = json.loads(path.read_text(encoding="utf-8"))["envelope"]
        store_id = document["store_id"]
        # These fixtures record the state a store ended up in, and one of them is already
        # ``active``. A terms PUT that also asserts ``active`` is an activation request, and
        # is refused without an artifact — which is the behaviour the next few lines then
        # collect the artifact for.
        document = dict(document, activation="shadow")
        written = await onboarding_client.put(
            f"/stores/{store_id}/envelope", headers=headers, json=document
        )
        assert written.status_code == 200, f"{path.name}: {written.text}"
        assert written.json()["activation"] == "shadow"

        stored = onboarding_store.current(store_id)
        artifact = {
            "approver": "A. Merchant",
            "approved_at": "2026-02-02T10:05:00Z",
            "envelope_hash": approval_digest(stored),
        }
        live = await onboarding_client.put(
            f"/stores/{store_id}/envelope",
            headers={**headers, "X-Envelope-Approval": json.dumps(artifact)},
            json={"activation": "active"},
        )
        assert live.status_code == 200, f"{path.name}: {live.text}"
        assert live.json()["activation"] == "active"


async def test_the_recorded_interview_fixture_still_reaches_the_same_envelope_over_http(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_store: EnvelopeVersions,
    onboarding_transcript: Any,
    onboarding_golden: dict[str, Any],
) -> None:
    """The CLI's own fixture, PUT over HTTP, produces the CLI's own golden envelope."""
    headers = {"authorization": f"Bearer {onboarding_admin_token}"}
    store_id = onboarding_golden["store_id"]

    written = await onboarding_client.put(
        f"/stores/{store_id}/envelope", headers=headers, json=onboarding_transcript
    )
    assert written.status_code == 200, written.text
    # The wire spelling of the golden envelope: `to_contract()` is what every envelope read
    # serves, and it renders a Claim's absent optional fields as explicit nulls. Comparing
    # against the fixture through the same projection keeps the assertion about the ENVELOPE
    # rather than about pydantic's null policy.
    assert written.json() == Envelope.from_obj(onboarding_golden).to_contract().model_dump(
        mode="json"
    )


async def test_an_interview_for_another_store_is_refused(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_store: EnvelopeVersions,
    onboarding_transcript: Any,
) -> None:
    """An envelope is not moved between stores by a PUT, and neither is an interview."""
    refused = await onboarding_client.put(
        "/stores/somebody-else/envelope",
        headers={"authorization": f"Bearer {onboarding_admin_token}"},
        json=onboarding_transcript,
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == "wrong-store"


async def test_the_interview_routes_refuse_an_anonymous_caller(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_transcript: Any,
) -> None:
    """An interview writes the store's whole negotiating position. It is not world-writable."""
    for response in (
        await onboarding_client.put("/stores/x/envelope", json=onboarding_transcript),
        await onboarding_client.put("/stores/x/envelope", json={"activation": "active"}),
    ):
        assert response.status_code == 401, response.text


# ======================================================================================
# 7. Adversarial: the ways the new door could have become a way around the gate
# ======================================================================================
async def test_re_onboarding_a_live_store_drops_it_back_to_shadow(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approve-then-edit escalation, attempted through the INTERVIEW door.

    ``edit_envelope`` already refuses to carry an approval forward, and the interview must not
    be a second way in that skips it: a merchant who approved a 20% cap and then answers the
    interview again with 90% must not still be live on an approval nobody gave for 90%.
    """
    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    headers = {"authorization": f"Bearer {dash_admin_token}"}

    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]
    await dash_client.put(f"/stores/{STORE}/envelope", headers=headers, json=_transcript(script))
    approval = (await _page(dash_client, dash_admin_token))["onboarding"]["approval"]
    signed = dict(approval["artifact"], approver="A. Merchant", approved_at="2026-02-02T10:05:00Z")
    live = await dash_client.put(
        f"/stores/{STORE}/envelope",
        headers={**headers, "X-Envelope-Approval": json.dumps(signed)},
        json={"activation": "active"},
    )
    assert live.json()["activation"] == "active"

    greedier = _transcript(script)
    for turn in greedier["turns"]:
        if turn.get("role") == "merchant" and "Twenty percent" in turn["text"]:
            turn["text"] = "Actually, ninety percent — whatever it takes."
    again = await dash_client.put(f"/stores/{STORE}/envelope", headers=headers, json=greedier)
    assert again.status_code == 200, again.text
    assert again.json()["version"] == 2
    assert again.json()["max_discount_pct"] == 90.0
    assert again.json()["activation"] == "shadow", (
        "a re-answered interview kept the store live under an approval given for other terms"
    )
    assert dash_store.is_live(STORE) is False

    # And v1's approval does not activate v2: the version is inside the digest.
    stale = await dash_client.put(
        f"/stores/{STORE}/envelope",
        headers={**headers, "X-Envelope-Approval": json.dumps(signed)},
        json={"activation": "active"},
    )
    assert stale.status_code == 403, stale.text
    assert dash_store.is_live(STORE) is False


async def test_an_interview_that_also_asks_to_go_live_gets_neither_without_paperwork(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One request cannot be both the interview and its own approval."""
    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    headers = {"authorization": f"Bearer {dash_admin_token}"}

    script = (await _page(dash_client, dash_admin_token))["onboarding"]["interview"]
    both = dict(_transcript(script), activation="active")
    answered = await dash_client.put(f"/stores/{STORE}/envelope", headers=headers, json=both)

    assert answered.status_code == 403, answered.text
    assert answered.json()["error"] == "approval-required"
    # The safe half of the request is kept: the terms are on file, in shadow.
    assert dash_store.current(STORE).version == 1
    assert dash_store.current(STORE).activation == "shadow"
    assert dash_store.is_live(STORE) is False


async def test_a_malformed_cluster_taxonomy_names_the_variable_instead_of_a_500(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd environment variable degrades one question; it does not take the page down."""
    from merchant_svc.onboarding.script import INTENT_CLUSTERS_ENV

    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    monkeypatch.setenv(INTENT_CLUSTERS_ENV, "[{not json at all")

    panel = (await _page(dash_client, dash_admin_token))["onboarding"]
    assert panel["missing"] == [INTENT_CLUSTERS_ENV]
    assert INTENT_CLUSTERS_ENV in panel["detail"]
    # The exchange's own report still supplies what it knows, so the question is not empty for
    # a store with history — the variable is named for the clusters it would have added.
    options = [
        turn["options"]
        for turn in panel["interview"]["turns"]
        if turn.get("question") == "pursue_clusters"
    ][0]
    assert [option["cluster_id"] for option in options] == ["trail-running-shoes"]


async def test_a_store_id_that_is_not_a_usable_shop_handle_does_not_break_the_page(
    dash_client: httpx.AsyncClient,
    dash_admin_token: str,
    dash_store: EnvelopeVersions,
    dash_upstreams: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shop_domain_for`` builds a host from a path segment; a strange one must not 500."""
    _configure(monkeypatch)
    _stub_reads(dash_upstreams)
    response = await dash_client.get(
        "/stores/not a shop/dashboard",
        headers={"authorization": f"Bearer {dash_admin_token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["onboarding"]["observation_surface"]["registered"] is False
