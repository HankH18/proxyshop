"""A refusal on the external door has to be actionable — and it may not weaken the door to be it.

``POST /v1/bid-requests`` is how a store's agent, written by someone who does not have this
repository, joins the network. Driven by hand it answered three consecutive 422s against
plausible bodies, and every refusal was correct and useless:

1. ``{"buyer_ref": …, "segments": [], "signals": {}}`` → ``['body','profile','pseudonym'] Field
   required``. Nothing named the five keys the body does take.
2. a `Preference` without ``direction`` → ``Field required``. ``direction`` is a THREE-VALUE enum
   and none of the three appeared.
3. ``price_sensitivity`` inside ``profile.buckets`` → ``Extra inputs are not permitted``.
   `ProfileBuckets` is closed over exactly five keys and the refusal named none of them.

Those three bodies are :data:`GUESSED_BODY`, :data:`PREFERENCE_WITHOUT_DIRECTION` and
:data:`UNDECLARED_BUCKET` below, and they are the spine of this file. Every one of them is
still refused with 422 — that is graded first and separately, because a "helpful" refusal that
had started ACCEPTING one of them would be a regression on an unauthenticated door dressed up as
a fix.

SIX SECTIONS, AND WHY EACH IS GRADED THE WAY IT IS
---------------------------------------------------
**Nothing was weakened.** ``§ Still refused``. Status equality on the measured bodies, plus a
control that the door still answers a real solicitation with a real bid, plus a case asserting
that a body differing from the valid one by exactly one enum member is still refused — because
"the refusal now lists the permitted values" is one careless edit away from "the refusal now
permits any value".

**The three refusals now say what a valid body looks like.** ``§ ...are now actionable``. One
test per measured refusal, each asserting the specific vocabulary that refusal was missing, and
each also re-asserting that FastAPI's own per-field detail is still there and still precise.

**The vocabulary is DERIVED, never restated.** ``§ Derived, not restated``. Two directions. The
lists a refusal prints are compared to ``contracts.registry.protocol_schema()`` — the PUBLISHED
bundle, read independently of the model that did the refusing — so the two cannot silently
disagree. And the derivation is shown working on a schema that is not `BidRequest` at all
(:func:`_toy_client`), which is what distinguishes "walks the schema" from "has `maximize`,
`minimize`, `prefer` typed into it somewhere and happens to be right" — and, in a second toy
route, on a schema that reuses the real definition NAMES over members the protocol never had,
because a lookup keyed on `PreferenceDirection` survives the first check and fails only that
one.

**Nothing the caller sent comes back.** ``§ Quotes nothing back``. FastAPI's stock 422 embeds the
offending value under ``input``; this door was returning an attacker's email address to them and
turning ``1e400`` into an HTTP 500 while doing it. The sentinel sweep plants a distinctive string
in every position of the body and requires it to be absent from the response — not "absent from
``input``", absent from the whole document, which is the only version of that assertion that
survives someone later adding a field to the help.

**The example is real, and reachable from the door.** ``§ The example is real``. The published
contract and the served route publish the same body; the door is required to ACCEPT it (which
nothing checked before — validating against the schema is a weaker claim); the pointer a refusal
hands out is followed over HTTP into the document it names; and the refusals themselves are
validated against the closed 422 schema the served document publishes, so the shape that is
declared and the shape that is sent cannot drift apart.

**Help can only ever add.** ``§ Degrades, never faults``. The derivation runs inside a `try`, and
the test forces it to raise and requires the refusal to still be a refusal. An enrichment that
can turn a 422 into a 500 is strictly worse than no enrichment.
"""

from __future__ import annotations

import copy
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from contracts import BidRequest
from contracts.registry import protocol_schema
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from store_agent.main import create_app
from store_agent.solicitation import configure_solicitation
from store_agent.solicitation import refusal as refusal_module
from store_agent.solicitation.refusal import (
    MAX_IDENTIFIER_CHARS,
    MAX_VALIDATION_ERRORS,
    EnrichedRefusalRoute,
    closed_object,
    example_pointer,
    permitted_values,
)
from store_agent.solicitation.routes import CANONICAL_BID_REQUEST

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"
STORE_AGENT_CONTRACT = REPO_ROOT / "packages" / "contracts" / "openapi" / "store-agent.openapi.json"

BID_REQUESTS = "/v1/bid-requests"

#: Sent with every raw-bytes probe, so the door parses rather than 415s.
JSON_HEADERS = {"content-type": "application/json"}
STORE_ID = "store-alpha"
STORE_DOMAIN = "store-alpha.example.com"
CLUSTER = "cluster-warm-layers"

#: A string that appears in no schema, no example and no pydantic message, so finding it anywhere
#: in a response means it came from the request. Deliberately spelled like something a store would
#: mind leaking.
SENTINEL = "shopper-at-example-dot-com-9d41f0"


def context(*, activation: str = "active") -> dict[str, Any]:
    """The store context the merchant service hands over, for an ACTIVATED store.

    ``active`` is overlaid on the shipped artifact — which states ``shadow`` — for the same
    reason ``test_solicitation.py`` does it: R7 makes an un-activated store answer 204, and this
    file needs the 200 control to be a real bid rather than a decline.
    """
    fixture = json.loads(ENVELOPE_FIXTURE.read_text(encoding="utf-8"))
    return {
        "store_id": STORE_ID,
        "envelope": {**fixture["envelope"], "activation": activation},
        "catalog": fixture["catalog"],
        "live_state": {
            "prod-cap": {"in_stock": True, "units_left": 7},
            "prod-floor": {"in_stock": True, "units_left": 3},
        },
        "learned_policy": None,
        "store_domain": STORE_DOMAIN,
    }


@pytest.fixture
def client() -> TestClient:
    """The real served app, through the real router. ``raise_server_exceptions=False`` so a 500
    is REPORTED as a 500 rather than re-raised out of the client — the fault this file grades
    away is one the door used to answer, and a test that explodes instead of asserting a status
    cannot tell 500 from 422."""
    app = create_app()
    configure_solicitation(app, context=context())
    return TestClient(app, raise_server_exceptions=False)


def valid_body(**overrides: Any) -> dict[str, Any]:
    """A `BidRequest` this door answers 200 to, for this store's catalogue and cluster."""
    body: dict[str, Any] = {
        "auction_id": "auc-0001",
        "intent": {
            "intent_id": "int-0001",
            "cluster_id": CLUSTER,
            "query": "a warm mid-layer for cold commutes",
            "hard_constraints": [],
            "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
            "ship_to": "US",
            "created_at": "2026-01-01T00:00:00Z",
            "schema_version": "2.0.0",
        },
        "profile": {
            "pseudonym": "psn-0001",
            "buckets": {
                "budget_band": "under-40",
                "category_affinity": ["supplements"],
                "region": "US",
                "first_time": True,
            },
        },
        "respond_by": "2099-01-01T00:00:00Z",
    }
    body.update(overrides)
    return body


#: Refusal (1), verbatim: an entirely reasonable guess at what a bid solicitation takes.
GUESSED_BODY: dict[str, Any] = {"buyer_ref": "buyer-9", "segments": [], "signals": {}}

#: Refusal (2): every field of a `Preference` except the one that is a closed vocabulary.
PREFERENCE_WITHOUT_DIRECTION = valid_body()
PREFERENCE_WITHOUT_DIRECTION["intent"] = {
    **PREFERENCE_WITHOUT_DIRECTION["intent"],
    "preferences": [{"field": "price", "weight": 1.0}],
}

#: Refusal (3): a bucket key that reads like one `ProfileBuckets` would have. It is closed
#: (R13 keeps identity fields out of it), so the key is refused rather than ignored.
UNDECLARED_BUCKET = valid_body()
UNDECLARED_BUCKET["profile"] = {
    "pseudonym": "psn-0001",
    "buckets": {"price_sensitivity": "high", "budget_band": "under-40"},
}

MEASURED_REFUSALS: dict[str, dict[str, Any]] = {
    "a guessed body": GUESSED_BODY,
    "a preference without a direction": PREFERENCE_WITHOUT_DIRECTION,
    "an undeclared bucket key": UNDECLARED_BUCKET,
}


def help_of(response: Any) -> dict[str, Any]:
    """The ``help`` object, asserted present. A refusal without it is the defect, not a variant."""
    assert response.status_code == 422, response.text
    body = response.json()
    assert "help" in body, f"a refusal on the external door carried no help at all: {body}"
    return body["help"]


def accepts_at(response: Any, location: list[Any]) -> list[str]:
    """The keys the refusal says the object at ``location`` accepts."""
    described = [obj for obj in help_of(response)["closed_objects"] if obj["loc"] == location]
    assert described, (
        f"the refusal described no closed object at {location}; it described "
        f"{[obj['loc'] for obj in help_of(response)['closed_objects']]}"
    )
    return described[0]["accepts"]


def values_at(response: Any, location: list[Any]) -> list[Any]:
    """The values the refusal says the field at ``location`` permits."""
    named = [entry for entry in help_of(response)["permitted_values"] if entry["loc"] == location]
    assert named, (
        f"the refusal named no permitted values at {location}; it named "
        f"{[entry['loc'] for entry in help_of(response)['permitted_values']]}"
    )
    return named[0]["values"]


# =============================================================================================
# § Still refused — the door did not get more permissive in exchange for getting more helpful.
# =============================================================================================


@pytest.mark.parametrize("name", sorted(MEASURED_REFUSALS))
def test_every_body_that_was_refused_before_is_still_refused_with_the_same_status(
    client: TestClient, name: str
) -> None:
    """The three measured bodies, unchanged, still 422.

    This is graded before anything about the message, and separately from it. The whole risk in
    "make the refusal more helpful" is that the refusal stops happening, and a suite that only
    asserted the new help would go green on a door that had started accepting `price_sensitivity`
    into a `ProfileBuckets` R13 keeps identity fields out of.
    """
    response = client.post(BID_REQUESTS, json=MEASURED_REFUSALS[name])
    assert response.status_code == 422, response.text


def test_a_body_differing_only_by_one_enum_member_is_still_refused(client: TestClient) -> None:
    """Publishing the permitted set may not publish a way past it.

    The refusal now prints ``["maximize","minimize","prefer"]``. A handler that derived that list
    and then, say, coerced an unknown direction to the first member would pass every assertion in
    the section below while silently ranking a bid under a preference the buyer never expressed.
    So: the valid body, one member changed to a word that is not in the set, must still be 422.
    """
    body = valid_body()
    body["intent"]["preferences"] = [{"field": "price", "direction": "maximise", "weight": 1.0}]

    response = client.post(BID_REQUESTS, json=body)

    assert response.status_code == 422, response.text
    assert values_at(response, ["body", "intent", "preferences", 0, "direction"]) == [
        "maximize",
        "minimize",
        "prefer",
    ]


def test_the_door_still_answers_a_real_solicitation_with_a_real_bid(client: TestClient) -> None:
    """The control. Without it every assertion above is satisfied by a door that refuses
    everything, which is the cheapest way to pass a suite about refusals."""
    response = client.post(BID_REQUESTS, json=valid_body())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["store_id"] == STORE_ID
    assert body["offer"]["checkout_url"].startswith(f"https://{STORE_DOMAIN}/")
    assert "help" not in body


# =============================================================================================
# § The three measured refusals are now actionable.
# =============================================================================================


def test_a_guessed_body_is_told_which_keys_the_door_accepts(client: TestClient) -> None:
    """Refusal (1). ``Field required`` × 4 named four fields and no shape.

    What the caller needed was the key set. All five are named, the required four are marked, and
    the nested objects they would then have to build are described too — a caller told only
    ``profile: Field required`` has to guess `BuyerProfile` next.
    """
    response = client.post(BID_REQUESTS, json=GUESSED_BODY)

    assert accepts_at(response, ["body"]) == [
        "auction_id",
        "intent",
        "product_ref",
        "profile",
        "respond_by",
    ]
    described = {tuple(obj["loc"]): obj for obj in help_of(response)["closed_objects"]}
    assert described[("body",)]["requires"] == ["auction_id", "intent", "profile", "respond_by"]
    assert described[("body", "profile")]["accepts"] == ["pseudonym", "buckets"]
    # The per-field detail FastAPI wrote is still there, unchanged and still precise.
    reported = {(entry["type"], tuple(entry["loc"])) for entry in response.json()["detail"]}
    assert ("missing", ("body", "profile")) in reported
    assert ("extra_forbidden", ("body", "buyer_ref")) in reported


def test_a_missing_enum_field_is_told_the_values_it_may_take(client: TestClient) -> None:
    """Refusal (2). ``direction`` is a three-value enum and the refusal named none of the three.

    A ``missing`` error is the case that makes the whole exercise worth doing: there is no
    offending value for the caller to look at and reason backwards from, so before this the
    refusal carried literally nothing about what belongs there.
    """
    response = client.post(BID_REQUESTS, json=PREFERENCE_WITHOUT_DIRECTION)

    location = ["body", "intent", "preferences", 0, "direction"]
    assert values_at(response, location) == ["maximize", "minimize", "prefer"]
    # And the object it sits in, so a caller who omitted two fields is not told about one.
    assert accepts_at(response, ["body", "intent", "preferences", 0]) == [
        "field",
        "direction",
        "weight",
    ]
    # The index survives as an INDEX. It is what the caller indexes their own payload with.
    assert response.json()["detail"][0]["loc"] == location


def test_an_undeclared_bucket_key_is_told_the_keys_the_object_does_accept(
    client: TestClient,
) -> None:
    """Refusal (3). ``additionalProperties: false`` over five keys, and the refusal named none.

    The strictness is the point — R13 is why `ProfileBuckets` is closed — so the fix is to say
    what the five are, never to let a sixth in. ``requires`` is empty here and that is itself
    information: every bucket is optional, so a caller who omits the lot is fine.
    """
    response = client.post(BID_REQUESTS, json=UNDECLARED_BUCKET)

    location = ["body", "profile", "buckets"]
    assert accepts_at(response, location) == [
        "budget_band",
        "category_affinity",
        "frequency_tier",
        "region",
        "first_time",
    ]
    described = [obj for obj in help_of(response)["closed_objects"] if obj["loc"] == location][0]
    assert described["requires"] == []
    assert response.json()["detail"][0]["loc"] == [*location, "price_sensitivity"]


# =============================================================================================
# § Derived, not restated — the lists cannot drift away from what actually validates.
# =============================================================================================


def test_the_permitted_values_are_the_published_schemas_own_enum(client: TestClient) -> None:
    """What the refusal prints and what ``protocol.schema.json`` states are the same list.

    Read here from ``contracts.registry.protocol_schema()`` — the published bundle — rather than
    from the model that did the refusing, so this is two independent readings agreeing and not
    one reading compared with itself.
    """
    published = list(protocol_schema()["$defs"]["PreferenceDirection"]["enum"])

    response = client.post(BID_REQUESTS, json=PREFERENCE_WITHOUT_DIRECTION)

    assert values_at(response, ["body", "intent", "preferences", 0, "direction"]) == published


@pytest.mark.parametrize(
    ("definition", "body", "location"),
    [
        ("BidRequest", GUESSED_BODY, ["body"]),
        ("ProfileBuckets", UNDECLARED_BUCKET, ["body", "profile", "buckets"]),
        ("Preference", PREFERENCE_WITHOUT_DIRECTION, ["body", "intent", "preferences", 0]),
    ],
)
def test_the_accepted_keys_are_the_published_schemas_own_properties(
    client: TestClient, definition: str, body: dict[str, Any], location: list[Any]
) -> None:
    """Same two-reading rule for the closed key sets, on all three objects this file touches."""
    published = protocol_schema()["$defs"][definition]
    assert published["additionalProperties"] is False, (
        f"{definition} is no longer a closed object in the published bundle; the refusal would "
        "be describing a key set that is not an allowlist any more"
    )

    response = client.post(BID_REQUESTS, json=body)

    assert accepts_at(response, location) == list(published["properties"])


class Setting(StrEnum):
    """A closed vocabulary that exists nowhere in this platform's protocol."""

    wobble = "wobble"
    yaw = "yaw"


class Dial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setting: Setting


class Panel(BaseModel):
    """A closed object sharing not one property name with anything in `protocol.schema.json`.

    Module level rather than nested inside the fixture below, and that is load-bearing: this file
    carries ``from __future__ import annotations``, so a handler's parameter annotation is a
    STRING that FastAPI resolves against the handler's module globals. A model defined inside a
    function is not in those globals, and the route would silently end up with no body model —
    which is indistinguishable, from the outside, from the derivation this test exists to prove.
    """

    model_config = ConfigDict(extra="forbid")
    dial: Dial
    label: str | None = None


class PreferenceDirection(StrEnum):
    """**The real protocol name, deliberately, over members the protocol does not have.**

    `contracts.PreferenceDirection` is ``maximize``/``minimize``/``prefer``, and a model here that
    merely used unfamiliar names could not tell a schema walk apart from a lookup keyed on the
    definition NAME. An adversarial verifier demonstrated exactly that gap: a shortcut returning
    the real three members for any ``$ref`` ending in ``PreferenceDirection``, left sitting on top
    of the general walker, passed the whole suite. This class is a `PreferenceDirection` whose
    members are ``port``/``starboard``, so a walk answers ``["port", "starboard"]`` and anything
    keyed on the name answers the protocol's three.
    """

    port = "port"
    starboard = "starboard"


class ProfileBuckets(BaseModel):
    """Likewise a real protocol name over properties `ProfileBuckets` does not declare."""

    model_config = ConfigDict(extra="forbid")
    heading: PreferenceDirection
    knots: int | None = None


class Helm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buckets: ProfileBuckets


def _toy_client() -> TestClient:
    """A door whose schema shares NOT ONE member with `BidRequest`, on the same route class.

    This is the falsification. Everything above is also satisfied by a module with the real
    vocabulary hard-coded in it, because the real vocabulary is what the real schema says. Only a
    schema the implementation has never seen can tell "derives the lists" apart from "was written
    while looking at them".
    """
    router = APIRouter(route_class=EnrichedRefusalRoute)

    @router.post("/toy")
    def toy(panel: Panel) -> dict[str, str]:  # pragma: no cover - only refusals are driven
        return {"ok": panel.dial.setting.value}

    @router.post("/helm")
    def helm(helm: Helm) -> dict[str, str]:  # pragma: no cover - only refusals are driven
        return {"ok": helm.buckets.heading.value}

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_the_help_describes_whatever_schema_the_route_validates_with() -> None:
    """The vocabulary follows the ROUTE's model, not a list living in the refusal module.

    A ``wobble``/``yaw`` enum and a ``dial``/``label`` object appear nowhere in this repository's
    protocol. If the refusal can name them, it read them off the model FastAPI validated with;
    if it could only name `PreferenceDirection`, every assertion in the section above was
    grading a coincidence.
    """
    toy = _toy_client()

    response = toy.post("/toy", json={"dial": {"setting": "spin"}, "colour": "red"})

    assert response.status_code == 422, response.text
    help_block = response.json()["help"]
    assert help_block["request_schema"] == "Panel"
    assert {"wobble", "yaw"} == {
        value
        for entry in help_block["permitted_values"]
        if entry["loc"] == ["body", "dial", "setting"]
        for value in entry["values"]
    }
    assert [obj["accepts"] for obj in help_block["closed_objects"] if obj["loc"] == ["body"]] == [
        ["dial", "label"]
    ]
    # And the pointer is this route's path, not the solicitation door's.
    assert help_block["example"]["pointer"].startswith("/paths/~1toy/post/")


def test_the_help_reads_the_schema_and_not_the_definition_name() -> None:
    """The stronger falsification: real protocol NAMES carrying members the protocol never had.

    ``test_the_help_describes_whatever_schema_the_route_validates_with`` proves a schema walk
    EXISTS. It does not prove the walk is the only path, and an adversarial verifier showed the
    difference: a shortcut returning ``["maximize","minimize","prefer"]`` for any ``$ref`` ending
    in ``PreferenceDirection``, layered ON TOP of the untouched general walker, passed that test
    and the whole suite with it.

    So this route's schema declares a ``PreferenceDirection`` of ``port``/``starboard`` and a
    ``ProfileBuckets`` of ``heading``/``knots``. Any answer keyed on a definition name reports the
    protocol's vocabulary here and fails; only a walk reports the schema's own.
    """
    toy = _toy_client()

    response = toy.post("/helm", json={"buckets": {"heading": "nor-nor-east", "sails": 2}})

    assert response.status_code == 422, response.text
    help_block = response.json()["help"]

    values = [e["values"] for e in help_block["permitted_values"] if e["loc"][-1] == "heading"]
    assert values == [["port", "starboard"]], (
        f"a `PreferenceDirection` was answered with {values} — that is the protocol's vocabulary, "
        "not this route's, so something is keyed on the definition name rather than walking it"
    )
    accepts = [o["accepts"] for o in help_block["closed_objects"] if o["loc"][-1] == "buckets"]
    assert accepts == [["heading", "knots"]], (
        f"a `ProfileBuckets` was answered with {accepts} rather than this schema's own properties"
    )
    # Belt and braces: neither protocol vocabulary appears anywhere in this refusal.
    for leaked in ("maximize", "minimize", "prefer", "budget_band", "category_affinity"):
        assert leaked not in response.text, f"{leaked!r} came from the protocol, not this schema"


def test_an_open_object_is_not_described_as_though_it_were_an_allowlist() -> None:
    """A schema that permits extra keys gets no ``accepts`` list.

    Printing the properties an OPEN object happens to declare would read to an integrator as
    "these and no others", which is the opposite of what such a schema says. The helper answers
    ``None``, and the fallback then describes nothing rather than describing it wrongly.
    """
    open_object = {"type": "object", "properties": {"a": {"type": "string"}}}
    closed = {**open_object, "additionalProperties": False}

    assert closed_object(open_object, {}) is None
    assert closed_object(closed, {}) == (["a"], [])


def test_an_optional_field_is_read_through_its_nullable_wrapper() -> None:
    """``str | None`` is emitted as ``anyOf: [{...}, {"type": "null"}]``.

    Half the fields on `ProfileBuckets` are spelled that way, so a walker that did not unwrap it
    would answer ``None`` for every optional enum in the protocol and the help would silently
    thin out exactly where a caller is most likely to be guessing.
    """
    nullable_enum = {"anyOf": [{"$ref": "#/$defs/Dir"}, {"type": "null"}]}
    defs = {"Dir": {"enum": ["up", "down"]}}

    assert permitted_values(nullable_enum, defs) == ["up", "down"]


def test_the_pointer_escapes_a_path_the_way_rfc_6901_does() -> None:
    """``~`` before ``/``. Reversing the two escapes double-escapes a tilde in a path."""
    assert example_pointer("/v1/bid-requests", "POST").startswith(
        "/paths/~1v1~1bid-requests/post/requestBody/"
    )
    assert example_pointer("/a~b/c", "GET").startswith("/paths/~1a~0b~1c/get/")


# =============================================================================================
# § Quotes nothing back — the refusal names the field, never the value.
# =============================================================================================


def _sentinel_bodies() -> dict[str, Any]:
    """One body per position a caller-chosen VALUE can occupy in a `BidRequest`."""
    at_root = valid_body(auction_id={"nested": SENTINEL})
    wrong_type = valid_body()
    wrong_type["intent"]["preferences"] = [{"field": "price", "direction": SENTINEL, "weight": 1.0}]
    wrong_shape = valid_body()
    wrong_shape["profile"]["buckets"]["category_affinity"] = SENTINEL
    extra_value = valid_body()
    extra_value["profile"]["buckets"] = {"budget_band": "under-40", "spend": SENTINEL}
    return {
        "a value where an object was expected": at_root,
        "a value outside a closed enum": wrong_type,
        "a value where a list was expected": wrong_shape,
        "a value under an undeclared key": extra_value,
        "a value under a whole body of undeclared keys": {"a": SENTINEL, "b": [SENTINEL]},
    }


#: The shortest run of sentinel characters that counts as an echo. A *partial* echo is still an
#: echo — 16 characters of an email address is an email address — and a containment check on the
#: WHOLE sentinel does not see one, which is how a truncated ``input`` appended to ``msg``
#: survives a naive sweep. Eight is short enough to catch a truncation worth having and long
#: enough that a collision with schema vocabulary would have to be deliberate.
MIN_ECHOED_RUN = 8


def echoed_fragments(text: str, value: str) -> list[str]:
    """Every run of ``MIN_ECHOED_RUN``+ characters of ``value`` that appears in ``text``."""
    windows = {
        value[start : start + MIN_ECHOED_RUN]
        for start in range(max(len(value) - MIN_ECHOED_RUN + 1, 0))
    }
    return sorted(window for window in windows if window in text)


@pytest.mark.parametrize("name", sorted(_sentinel_bodies()))
def test_no_value_from_the_request_appears_anywhere_in_the_refusal(
    client: TestClient, name: str
) -> None:
    """The proof obligation, asserted against the WHOLE response and in FRAGMENTS.

    ``apps/merchant/svc/src/collector/routes.py`` already holds this repo to the rule — "an error
    body is as much a place data lives as a database is" — and this unauthenticated door was not
    holding to it: a 3 KB body carrying an email address came back as a 15.7 KB refusal quoting
    it once per field error.

    Two things make the assertion hard to satisfy accidentally. It is scoped to the response as a
    whole rather than to ``input``, so it stays true when someone later adds a field to the help.
    And it looks for FRAGMENTS: an adversarial verifier killed the whole-string version of this
    test by appending ``str(error["input"])[:16]`` to ``msg`` — sixteen characters of every value
    the caller sent, coming back on a route whose entire point is that it sends none of them, with
    the suite still green. A containment check on the full sentinel cannot see that; this can.
    """
    body = _sentinel_bodies()[name]

    response = client.post(BID_REQUESTS, json=body)

    assert response.status_code == 422, response.text
    fragments = echoed_fragments(response.text, SENTINEL)
    assert fragments == [], (
        f"the refusal quoted {len(fragments)} fragment(s) of the caller's own value back "
        f"({fragments[:3]}): {response.text[:400]}"
    )
    # Armed: the sentinel really was in what was sent, so its absence means something — and the
    # fragment finder really does find fragments, so an empty result is a fact and not a bug.
    assert SENTINEL in json.dumps(body)
    assert echoed_fragments(f"prefix {SENTINEL[3:24]} suffix", SENTINEL) != []


def test_the_refusal_does_not_grow_with_the_body_it_refuses(client: TestClient) -> None:
    """A refusal that scales with the request is an amplifier pointed at whoever asks for it.

    Measured before: 3 KB in, 15.7 KB out. The bound is the number of errors reported and the
    length of any one identifier, both of which are constants in this package rather than
    functions of the input.

    **One key is deliberately longer than the bound.** The first version of this body had a
    longest key of 36 characters, so the ``<= MAX_IDENTIFIER_CHARS`` assertion below never came
    near 128 and deleting the truncation from `validation_detail` left the suite green — an
    adversarial verifier found exactly that. The ``loc`` segment naming an undeclared key is the
    ONE caller-derived string this door emits, so the bound on it has to be graded by a key that
    actually exceeds it, and the truncation asserted to have happened rather than merely to have
    been possible.
    """
    # FIRST in the body, because only the first `MAX_VALIDATION_ERRORS` errors are reported and
    # pydantic reports extras in the order it met them — appended last, this key never reached
    # the refusal at all and the assertion below silently graded nothing.
    overlong = "z" * (MAX_IDENTIFIER_CHARS * 3)
    hostile: dict[str, Any] = {overlong: 1}
    hostile.update({f"{SENTINEL}-{index}": "x" * 500 for index in range(200)})

    response = client.post(BID_REQUESTS, json=hostile)

    assert response.status_code == 422, response.text
    assert len(response.text) < len(json.dumps(hostile)), (
        f"refusing {len(json.dumps(hostile))} bytes cost {len(response.text)} bytes of response"
    )
    detail = response.json()["detail"]
    assert len(detail) <= MAX_VALIDATION_ERRORS
    segments = [str(part) for entry in detail for part in entry["loc"]]
    assert max(len(segment) for segment in segments) <= MAX_IDENTIFIER_CHARS

    # And the overlong key really did reach the refusal, so the bound above was exercised rather
    # than merely satisfied by a body that never approached it.
    truncated = [segment for segment in segments if segment.startswith("zzzz")]
    assert truncated, (
        "the 384-character key never appeared in the refusal, so nothing here grades the "
        f"identifier bound; segments seen: {sorted(set(segments))[:5]}"
    )
    assert all(len(segment) == MAX_IDENTIFIER_CHARS for segment in truncated)


def test_a_pydantic_message_longer_than_the_ceiling_is_cut_to_it() -> None:
    """`validation_detail` is graded directly, because no real body can reach these bounds.

    Every message pydantic emits for a malformed `BidRequest` is under 62 characters and the
    longest legitimate property name is far under 128, so an HTTP-level test cannot exercise
    either ceiling — and both were consequently unarmed: deleting either slice left the suite
    green. The ceilings exist for input this package did not write, so they are graded with input
    this package did not write.
    """
    detail = refusal_module.validation_detail(
        [
            {
                "type": "T" * 500,
                "loc": ("body", "L" * 500, 3),
                "msg": "M" * 500,
                "input": {"never": "quoted"},
                "ctx": {"never": "quoted"},
            }
        ]
    )

    assert len(detail) == 1
    entry = detail[0]
    assert len(entry["msg"]) == refusal_module.MAX_VALIDATION_MESSAGE_CHARS
    assert len(entry["type"]) == MAX_IDENTIFIER_CHARS
    assert entry["loc"] == ["body", "L" * MAX_IDENTIFIER_CHARS, 3]
    # An index stays an index; and neither of the two dropped keys survives.
    assert entry["loc"][2] == 3 and isinstance(entry["loc"][2], int)
    assert set(entry) == {"type", "loc", "msg"}


def test_an_error_carrying_ctx_is_refused_without_it(client: TestClient) -> None:
    """``ctx`` is dropped, and this is what makes that a fact rather than an argument.

    It was unpinned in both directions and an adversarial verifier walked straight through the
    gap: no test sent a body whose errors carry ``ctx`` at all, so re-adding the key left the
    suite green — and ``REFUSAL_SCHEMA``'s ``additionalProperties: false`` never fired either,
    because ``missing`` and ``extra_forbidden`` (all three measured refusals produce only those)
    carry no ``ctx``.

    Both bodies here produce errors that DO carry one: an out-of-set enum value gives
    ``ctx.expected``, and unparseable JSON gives ``ctx.error``. The first is the case that
    matters most — ``expected`` is the same fact `help.permitted_values` states, derived, so if
    ``ctx`` ever came back the refusal would carry two spellings of one closed set.
    """
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - test-only dependency

    bad_enum = valid_body()
    bad_enum["intent"]["preferences"] = [{"field": "price", "direction": "sideways", "weight": 1.0}]
    probes = {
        "an out-of-set enum value": client.post(BID_REQUESTS, json=bad_enum),
        "unparseable JSON": client.post(
            BID_REQUESTS, content=b'{"auction_id": "unterminated', headers=JSON_HEADERS
        ),
    }
    validator = Draft202012Validator(_published_refusal_schema(client))

    for label, response in probes.items():
        assert response.status_code == 422, f"{label}: {response.text}"
        assert "ctx" not in response.text, f"{label}: {response.text[:300]}"
        for entry in response.json()["detail"]:
            assert set(entry) == {"type", "loc", "msg"}, f"{label}: {entry}"
        assert validator.is_valid(response.json()), f"{label}: {response.text[:300]}"

    # Armed: pydantic really does attach a ctx to both of these, so their absence above is this
    # module's doing and not an accident of which errors happened to be produced.
    with pytest.raises(PydanticValidationError) as raised:
        BidRequest.model_validate(bad_enum)
    assert any("ctx" in error for error in raised.value.errors())


def test_a_lone_surrogate_key_is_a_refusal_and_not_a_server_fault(client: TestClient) -> None:
    """A third 500 the echo was causing, found by an adversarial verifier and not by me.

    ``{"\\ud800bad": 1}`` is a lone UTF-16 surrogate: legal in a JSON document, not encodable as
    UTF-8. Quoting it back raised ``UnicodeEncodeError: surrogates not allowed`` inside
    ``JSONResponse.render`` and this unauthenticated door answered HTTP 500. It is the same root
    cause as ``1e400`` — the response could not render a value the request chose — and dropping
    ``input`` closed it without anything here being aimed at it.
    """
    response = client.post(BID_REQUESTS, content=b'{"\\ud800bad": 1}', headers=JSON_HEADERS)

    assert response.status_code == 422, response.text
    assert "help" in response.json()


def test_a_value_the_echo_could_not_encode_is_a_refusal_and_not_a_server_fault(
    client: TestClient,
) -> None:
    """``1e400`` is legal RFC-8259 JSON, parses to ``inf``, and used to answer HTTP 500.

    The fault was in RENDERING the echo, and the traceback was captured rather than guessed at:
    ``starlette/responses.py:195`` calls ``json.dumps(..., allow_nan=False)`` on a body carrying
    the quoted ``inf`` and raises ``ValueError: Out of range float values are not JSON
    compliant``. On a door with no authentication in front of it that is a way to make the agent
    answer 5xx with a one-line request, so it is graded by status and not by message.
    """
    body = (
        '{"auction_id":"a","respond_by":"2099-01-01T00:00:00Z","intent":{"intent_id":"i",'
        '"query":"q","hard_constraints":[],"preferences":[{"field":"price",'
        '"direction":"minimize","weight":1e400}],"created_at":"2026-01-01T00:00:00Z",'
        '"schema_version":"2.0.0"},"profile":{"pseudonym":"p","buckets":{"nope":1e400}}}'
    )

    response = client.post(BID_REQUESTS, content=body.encode(), headers=JSON_HEADERS)

    assert response.status_code == 422, response.text
    assert "1e400" not in response.text and "Infinity" not in response.text


@pytest.mark.parametrize("depth", [100, 1000, 2000, 5000, 20000])
def test_no_nesting_depth_makes_this_door_answer_a_server_fault(
    client: TestClient, depth: int
) -> None:
    """Depth is not size, so no byte ceiling closes this; dropping the echo does.

    A 4 KB body of 2 000 nested arrays answered HTTP 500, because ``jsonable_encoder`` recursed
    walking the quoted value inside FastAPI's own validation-exception handler. Past the JSON
    parser's own depth limit FastAPI answers 400 ("There was an error parsing the body") on its
    own, which is why the assertion is "a refusal, whichever refusal" rather than a single status.
    """
    body = '{"auction_id":' + "[" * depth + "]" * depth + "}"

    response = client.post(BID_REQUESTS, content=body.encode(), headers=JSON_HEADERS)

    assert 400 <= response.status_code < 500, response.text[:200]


# =============================================================================================
# § The example is real, and it is reachable from the door itself.
# =============================================================================================


def test_the_published_contract_and_this_route_publish_the_same_example() -> None:
    """One example, two places, and a test that refuses to let them drift.

    ``packages/contracts/generated/`` is generated and ``packages/contracts/openapi/`` is the
    checked-in contract, so the constant this route serves is a duplicate by necessity. A
    duplicate is only safe while something compares it.
    """
    contract = json.loads(STORE_AGENT_CONTRACT.read_text(encoding="utf-8"))
    published = contract["paths"][BID_REQUESTS]["post"]["requestBody"]["content"][
        "application/json"
    ]["example"]

    assert published == CANONICAL_BID_REQUEST


def test_the_canonical_example_is_a_body_this_door_actually_accepts(client: TestClient) -> None:
    """Schema-legal and door-accepted are different claims, and only the second one helps.

    ``packages/contracts/tests/test_openapi_contracts.py`` already proves the example validates
    against `BidRequest`. Nothing proved the DOOR takes it — a route may validate further than
    its schema does, and an example an integrator copies and gets a 422 for is worse than none.

    A 204 is an acceptance: the body was read and answered, and the decline is about this
    store's catalogue rather than about the shape. What is refused is 422.
    """
    response = client.post(BID_REQUESTS, json=CANONICAL_BID_REQUEST)

    assert response.status_code != 422, response.text
    assert response.status_code in {200, 204}, response.text


def test_the_example_check_would_notice_an_example_that_stopped_being_accepted(
    client: TestClient,
) -> None:
    """The arming control for the test above, which would otherwise pass on any 2xx-shaped door.

    One key removed from the published example and the door must refuse it — so "not a 422" is a
    fact about this example and not a property of the route.
    """
    broken = copy.deepcopy(CANONICAL_BID_REQUEST)
    del broken["profile"]

    assert client.post(BID_REQUESTS, json=broken).status_code == 422


def _published_refusal_schema(client: TestClient) -> dict[str, Any]:
    """The 422 body schema, read back out of the document the SERVED process publishes.

    Read over HTTP rather than imported from the module, so what is graded is what an integrator
    would actually be handed. A schema that only exists as a Python constant is not published.
    """
    document = client.get("/openapi.json").json()
    content = document["paths"][BID_REQUESTS]["post"]["responses"]["422"]["content"]
    return dict(content["application/json"]["schema"])


@pytest.mark.parametrize("name", sorted(MEASURED_REFUSALS))
def test_a_real_refusal_validates_against_the_schema_this_door_publishes_for_it(
    client: TestClient, name: str
) -> None:
    """The published 422 shape and the served 422 shape are the same shape.

    Overriding ``responses[422]`` REPLACES FastAPI's default entry rather than merging with it,
    so it is easy to publish a refusal that nothing declares — this door briefly did, carrying a
    description and no schema at all. Declaring one only helps if something checks it, and this
    is that check: real bodies, the real door, the real document.

    ``REFUSAL_SCHEMA`` is ``additionalProperties: false`` throughout, so this fails when a field
    is added to the response without being added to the published shape. That is the direction
    drift actually travels.
    """
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - test-only dependency

    response = client.post(BID_REQUESTS, json=MEASURED_REFUSALS[name])
    assert response.status_code == 422, response.text

    errors = sorted(
        Draft202012Validator(_published_refusal_schema(client)).iter_errors(response.json()),
        key=lambda error: list(error.path),
    )

    assert errors == [], [f"{list(e.path)}: {e.message}" for e in errors]


def test_the_published_refusal_schema_would_notice_an_undeclared_field(
    client: TestClient,
) -> None:
    """The arming control: the schema above must REJECT a body it does not declare.

    Without this, `test_a_real_refusal_validates_...` passes just as well against a permissive
    schema — and a permissive schema is exactly what a hand-written one decays into. So a real
    refusal is taken, one undeclared key is added to it, and the published schema must refuse it.
    """
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - test-only dependency

    refusal = client.post(BID_REQUESTS, json=UNDECLARED_BUCKET).json()
    validator = Draft202012Validator(_published_refusal_schema(client))

    assert validator.is_valid(refusal)

    smuggled = {**refusal, "input": {"price_sensitivity": "high"}}
    assert not validator.is_valid(smuggled), (
        "the published 422 schema accepts a refusal carrying the caller's input back; it is not "
        "closed, so it cannot notice the field this whole change exists to remove"
    )
    nested = copy.deepcopy(refusal)
    nested["detail"][0]["input"] = "high"
    assert not validator.is_valid(nested)


def test_the_pointer_a_refusal_hands_out_resolves_in_the_document_it_names(
    client: TestClient,
) -> None:
    """A pointer at a file in this repository would be useless to the caller this door exists for.

    So the refusal names ``/openapi.json`` — a document the same process serves, on the same
    host the caller just reached — and a JSON Pointer into it. This test follows both, over HTTP,
    exactly as an integrator would: fetch the document the refusal names, walk the pointer it
    gives, and require a body the door then accepts to be sitting there.
    """
    help_block = help_of(client.post(BID_REQUESTS, json=GUESSED_BODY))

    document = client.get(help_block["example"]["document"])
    assert document.status_code == 200, help_block["example"]["document"]

    node: Any = document.json()
    for raw in help_block["example"]["pointer"].split("/")[1:]:
        segment = raw.replace("~1", "/").replace("~0", "~")
        assert segment in node, f"the pointer left the document at {segment!r}"
        node = node[segment]

    assert node == CANONICAL_BID_REQUEST
    assert client.post(BID_REQUESTS, json=node).status_code != 422


# =============================================================================================
# § Degrades, never faults — help is an addition, so it may never be a reason a refusal stops.
# =============================================================================================


def test_help_that_cannot_be_derived_costs_the_caller_help_and_nothing_else(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the derivation to raise; the refusal must still be the refusal.

    The one thing worse than a 422 that does not help is a 422 that became a 500 while trying to,
    and this door has already shipped that exact failure once — in the other direction, through
    the ``input`` echo it no longer emits.
    """

    def explode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("a shape this walker does not understand")

    monkeypatch.setattr(refusal_module, "schema_help", explode)

    response = client.post(BID_REQUESTS, json=UNDECLARED_BUCKET)

    assert response.status_code == 422, response.text
    body = response.json()
    assert "help" not in body
    assert body["detail"][0]["loc"] == ["body", "profile", "buckets", "price_sensitivity"]
    assert SENTINEL not in response.text
