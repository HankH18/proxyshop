"""What a 422 on ``POST /v1/bid-requests`` says — and what it refuses to say back.

``POST /v1/bid-requests`` is the EXTERNAL DOOR of this product: it is how a store's own agent,
written by someone outside this repo, joins the network. Driven by hand with a plausible body,
it answered three consecutive 422s before accepting anything:

1. ``['body','profile','pseudonym'] Field required`` / ``['body','profile','buckets'] Field
   required`` — against ``{"buyer_ref": …, "segments": [], "signals": {}}``, a reasonable guess;
2. ``['body','intent','preferences',0,'direction'] Field required`` — ``direction`` is a
   THREE-VALUE enum and the refusal named none of the three;
3. ``['body','profile','buckets','price_sensitivity'] Extra inputs are not permitted`` —
   `ProfileBuckets` is ``additionalProperties: false`` over exactly five keys and the refusal
   named none of them.

**Every one of those refusals was correct.** Not one of them told an integrator what a valid body
looks like, and an integrator who does not already have the schema in front of them cannot get
in. That is what this module fixes, and it fixes it WITHOUT relaxing anything: the per-field
`loc`/`msg`/`type` FastAPI produces is passed through unchanged and every body refused before is
refused now, with the same 422.

THE TWO THINGS ADDED, AND WHY NEITHER CAN DRIFT
-----------------------------------------------
Under ``help`` the refusal carries the vocabulary the caller needed:

* ``permitted_values`` — for a field whose schema is a closed set of values, the values. Answers
  (2): a missing or wrong ``direction`` now comes back with ``["maximize","minimize","prefer"]``.
* ``closed_objects`` — for an object that is ``additionalProperties: false``, the keys it DOES
  accept and which of them are required. Answers (1) and (3).
* ``example`` — a JSON Pointer into this service's OWN ``/openapi.json``, at the canonical valid
  body ``store_agent.solicitation.routes.CANONICAL_BID_REQUEST`` publishes there. A pointer at a
  file in this repository would be useless to the integrator this door exists for; a pointer at
  the document the running process already serves is one they can follow with `curl`.

Both lists are DERIVED, at request time, from ``model.model_json_schema()`` of the model the
route validates with — reached through :attr:`APIRoute.body_field`, so it is not this module's
opinion about which model that is either. Nothing here hand-copies an enum member or a property
name. The alternative — restating `PreferenceDirection` as a tuple next to the handler — is a
second spelling of a closed set, and a second spelling is a place to drift: the day a member is
added to the published schema, the hand-written list keeps refusing a body the door accepts and
tells the integrator the opposite of the truth. What is derived from the validator cannot
disagree with the validator. ``test_solicitation_refusal.py`` pins the derivation to the
PUBLISHED bundle as well (``contracts.registry.protocol_schema()``), so the same lists are
proved equal to what ``protocol.schema.json`` states, in both directions.

AND THE THING REMOVED: ``input``
--------------------------------
FastAPI's stock 422 quotes the offending value back under ``input``. This module drops it, and
that is not tidying — it was measured on this door, unauthenticated, at HEAD:

* ``{"shopper@example.com": "…"}`` came back in the refusal, once per field error: a 3 KB body
  produced a 15.7 KB response quoting an email address the store agent has no business holding.
  ``apps/merchant/svc/src/collector/routes.py`` already holds this repo to the rule — "an error
  body is as much a place data lives as a database is" — and this door was not holding to it.
* **Three separate HTTP 500s**, all of them faults in HANDLING the echo rather than in reading
  the request, and all three tracebacks captured rather than guessed at::

      1e400            starlette/responses.py:195   json.dumps(..., allow_nan=False)
                       ValueError: Out of range float values are not JSON compliant: inf
      depth 2 000      fastapi/exception_handlers.py:25 -> fastapi/encoders.py:322
                       RecursionError: maximum recursion depth exceeded
      {"\\ud800bad":1}  starlette/responses.py:195   JSONResponse.render
                       UnicodeEncodeError: surrogates not allowed

  The first and third are Starlette RENDERING the quoted value; the second is
  ``jsonable_encoder`` WALKING it. None is reachable without ``input``, which is why dropping it
  closes all three and why no separate guard is needed for any of them. A validation refusal the
  body can turn into a server fault is not a refusal, and on a door with no authentication in
  front of it it is a way to make the agent answer 5xx with a one-line request.

``ctx`` goes too, and for a plainer reason than the value echo: everything this module would have
wanted from it (``expected``, on an enum error) is derived from the schema below with more
precision, and what is left is error-shaped text this package did not write, on a route where
every byte returned should be one it chose to return. It is NOT dropped because it quotes the
caller's bytes — measured, a ``json_invalid`` ``ctx`` is positional ("EOF while parsing a string
at line 1 column 48") and quotes nothing.

What survives is where the problem is (``loc``), what it is (``msg``), its machine token
(``type``), and — new — what would have been accepted. **The FIELD is named; the value never is.**
A key IS named, in ``loc``, and that is a deliberate line rather than an oversight: this object
is a CLOSED schema, so "``price_sensitivity`` is not a key here, these five are" is the whole fix
instruction, where the collector's beacon takes arbitrary caller-chosen keys and can only report
a count. :data:`MAX_IDENTIFIER_CHARS` bounds it. Note precisely what that buys: the refusal no
longer SCALES with the request — the stock door grew about 6.7× linearly, and the largest refusal
observed across every probe here is 4 468 bytes — but it is still a bounded amplifier, 0 bytes in
to 488 bytes out. Bounded is the property; small is not.

WHY A ROUTE CLASS AND NOT ``@app.exception_handler``
-----------------------------------------------------
``store_agent.main`` is orchestrator-owned and FROZEN (B6(iii)), so the app-level handler is not
available to this package. It would be the wrong tool anyway, for the reason
``apps/exchange/src/auction/routes.py`` records: ``add_exception_handler`` mutates one app
object, so an installation performed while this module is imported reaches every app built
afterwards and none built before, and this service builds apps in tests, in
``proxyshop_support.asgi_server`` and at module import. A route class travels with the ROUTER, is
applied by ``include_router``, and is therefore build-order independent — and it is scoped to the
door it is about, rather than silently changing the sibling trust-intake router this ticket does
not own.

**Enrichment can only ever ADD.** :func:`refusal_body` computes the help inside a `try`, and any
failure — a model whose schema will not build, a shape this walker does not understand — falls
back to the plain input-free detail. The one thing worse than a 422 that does not help is a 422
that became a 500 while trying to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from functools import lru_cache
from typing import Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

__all__ = [
    "BODY_LOC",
    "DEFAULT_OPENAPI_URL",
    "MAX_IDENTIFIER_CHARS",
    "MAX_VALIDATION_ERRORS",
    "MAX_VALIDATION_MESSAGE_CHARS",
    "PACKAGE_CONTRACT",
    "REFUSAL_SCHEMA",
    "EnrichedRefusalRoute",
    "closed_object",
    "example_pointer",
    "node_at",
    "permitted_values",
    "refusal_body",
    "schema_help",
    "validation_detail",
]

_log = logging.getLogger(__name__)

#: The status a body that does not validate is refused with. Unchanged, and stated as a constant
#: so the one thing this module must NOT alter is visible in one place: every body the stock
#: handler refused is refused here, with this.
HTTP_UNPROCESSABLE = 422

#: The most per-field errors one refusal carries. A body with 5 000 bad fields is 5 000 error
#: objects and the caller only ever needed to know the request was unprocessable; twenty is more
#: than anyone debugging a client reads at once. Matches
#: ``buyer_svc.intent.routes.MAX_VALIDATION_ERRORS``, which was set for the same door-shaped
#: reason on the buyer side.
MAX_VALIDATION_ERRORS = 20

#: The most characters of one pydantic message this router passes on. A ceiling on a string this
#: package did not write, not a budget it expects to spend — and measured, so that "the per-field
#: detail is passed through unchanged" is a fact rather than a hope: over every distinct
#: ``(type, msg)`` pydantic produces for a malformed `BidRequest`, the longest is 62 characters
#: ("Input should be a valid dictionary or instance of BuyerProfile"). Nothing reachable on this
#: door is truncated; the cap exists for the messages a future schema change might bring.
MAX_VALIDATION_MESSAGE_CHARS = 200

#: The most characters of one ``loc`` segment or error ``type``. The segment naming an
#: ``extra_forbidden`` key is the caller's own text — the only caller-derived text this module
#: emits — so it is bounded. 128 is ``buyer_svc.intent.models.MAX_IDENTIFIER_LENGTH``, the
#: length this platform already calls an identifier.
MAX_IDENTIFIER_CHARS = 128

#: FastAPI's first ``loc`` segment for anything that came out of the request body. Errors that do
#: not start here (a header, a query parameter) are passed through without help, because the
#: schema walked below is the BODY's schema and a path into it would mean nothing.
BODY_LOC = "body"

#: Where the served schema lives when the application does not say. FastAPI's own default; read
#: off ``app.openapi_url`` first, so a deployment that moves it is pointed at the right place.
DEFAULT_OPENAPI_URL = "/openapi.json"

#: The checked-in contract this package's door is declared in, named in every refusal for the
#: reader who HAS this repository. The served ``/openapi.json`` pointer beside it is the one that
#: matters to an integrator who does not. Stated as a constant because it is the one string in
#: the help that is not derived from the route: this class is the solicitation door's, and a
#: future reuse on a route declared in a different document has to change it.
PACKAGE_CONTRACT = "packages/contracts/openapi/store-agent.openapi.json"

#: A ``loc``: property names and array indices, exactly as :func:`validation_detail` emits them.
#: Spelled once and referenced by every place below that carries one, so the three cannot drift
#: into three different opinions about whether an index is a string.
_LOC_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
}

#: **The shape of a 422 from this door**, declared so the served ``/openapi.json`` says what the
#: body actually is rather than only describing it in prose.
#:
#: Overriding ``responses[422]`` on the route REPLACES FastAPI's default entry rather than
#: merging into it, so supplying only a ``description`` published a refusal with no schema at
#: all — a contract that had got quieter in the same change that was meant to make the door
#: easier to integrate against. This is that schema.
#:
#: ``additionalProperties: false`` at every level is what makes it a gate instead of a
#: description. ``test_solicitation_refusal.py`` drives real bad bodies through the door, pulls
#: this schema back out of the SERVED document, and validates the refusals against it — so a
#: field added to the body without being added here fails the build, which is the only way a
#: published shape and a served shape stay the same shape.
REFUSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "EnrichedValidationRefusal",
    "required": ["detail"],
    "additionalProperties": False,
    "properties": {
        "detail": {
            "type": "array",
            "description": "FastAPI's per-field errors. Never carries the value that was sent.",
            "items": {
                "type": "object",
                "required": ["type", "loc", "msg"],
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string"},
                    "loc": _LOC_SCHEMA,
                    "msg": {"type": "string"},
                },
            },
        },
        "help": {
            "type": "object",
            "description": "Derived from the schema that did the refusing. Absent if underivable.",
            "required": ["request_schema", "example", "closed_objects", "permitted_values"],
            "additionalProperties": False,
            "properties": {
                "request_schema": {"type": "string"},
                "example": {
                    "type": "object",
                    "required": ["document", "pointer", "contract"],
                    "additionalProperties": False,
                    "properties": {
                        "document": {"type": "string"},
                        "pointer": {"type": "string"},
                        "contract": {"type": "string"},
                    },
                },
                "closed_objects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["loc", "accepts", "requires"],
                        "additionalProperties": False,
                        "properties": {
                            "loc": _LOC_SCHEMA,
                            "accepts": {"type": "array", "items": {"type": "string"}},
                            "requires": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "permitted_values": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["loc", "values"],
                        "additionalProperties": False,
                        "properties": {
                            "loc": _LOC_SCHEMA,
                            "values": {
                                "type": "array",
                                "items": {
                                    "type": ["string", "number", "boolean", "null"],
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}

#: How deep :func:`_alternatives` will unwrap ``anyOf``/``oneOf`` nesting. Generated models nest
#: one level (``str | None``); the bound exists so a hand-written or future schema cannot turn a
#: refusal into unbounded recursion.
_MAX_ALTERNATIVE_DEPTH = 8

#: The prefix a local ``$ref`` in a pydantic-emitted schema carries. A ``$ref`` that is not local
#: is not followed: this module resolves against the model's own ``$defs`` and never fetches.
_LOCAL_REF = "#/$defs/"


def _deref(node: Any, defs: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """``node`` with any local ``$ref`` chain followed, or ``None`` if it is not a schema.

    Cycle-safe: `Intent` referring to itself through a chain would otherwise spin here, and a
    refusal path is the last place to discover that.
    """
    seen: set[str] = set()
    while isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
        ref: str = node["$ref"]
        if not ref.startswith(_LOCAL_REF):
            return None
        name = ref[len(_LOCAL_REF) :]
        if name in seen:
            return None
        seen.add(name)
        node = defs.get(name)
    return node if isinstance(node, Mapping) else None


def _alternatives(node: Any, defs: Mapping[str, Any], depth: int = 0) -> list[Mapping[str, Any]]:
    """The schemas ``node`` may actually be: ``$ref``s followed, ``anyOf`` flattened, null dropped.

    An optional field is emitted by pydantic as ``{"anyOf": [{"type": "string"}, {"type":
    "null"}]}``, so every question this module asks — "is this an enum", "is this a closed
    object", "what is inside it" — has to be asked of the branches rather than of the wrapper.
    Dropping the ``null`` branch is what makes ``budget_band: str | None`` answer the same as
    ``budget_band: str``; nullability is not what a caller reading a refusal is asking about.
    """
    resolved = _deref(node, defs)
    if resolved is None:
        return []
    if depth >= _MAX_ALTERNATIVE_DEPTH:
        return [resolved]
    for key in ("anyOf", "oneOf"):
        branches = resolved.get(key)
        if isinstance(branches, Sequence) and not isinstance(branches, (str, bytes)):
            found: list[Mapping[str, Any]] = []
            for branch in branches:
                for alternative in _alternatives(branch, defs, depth + 1):
                    if alternative.get("type") == "null":
                        continue
                    found.append(alternative)
            return found
    return [resolved]


def _child(node: Any, segment: Any, defs: Mapping[str, Any]) -> Any:
    """The schema of ``segment`` inside ``node``, or ``None`` when the schema does not have one.

    ``None`` is an ordinary answer and the common one on the path that matters: an
    ``extra_forbidden`` error names a key the schema does not declare, which is precisely why it
    was refused. The caller then asks the PARENT what it does accept.
    """
    for alternative in _alternatives(node, defs):
        if isinstance(segment, int) and not isinstance(segment, bool):
            items = alternative.get("items")
            if isinstance(items, Mapping):
                return items
            continue
        properties = alternative.get("properties")
        if isinstance(properties, Mapping) and segment in properties:
            return properties[segment]
    return None


def node_at(schema: Mapping[str, Any], path: Sequence[Any], defs: Mapping[str, Any]) -> Any:
    """The subschema at ``path`` under ``schema``, or ``None``.

    ``path`` is a FastAPI ``loc`` with its leading ``"body"`` already removed, so its segments are
    property names and array indices in the body's own vocabulary.
    """
    node: Any = schema
    for segment in path:
        node = _child(node, segment, defs)
        if node is None:
            return None
    return node


def permitted_values(node: Any, defs: Mapping[str, Any]) -> list[Any] | None:
    """The closed set of values ``node`` accepts, or ``None`` when it is not a closed set.

    Reads ``enum`` and single-valued ``const``, which is how every closed vocabulary in
    ``protocol.schema.json`` is spelled. Only JSON scalars are returned: a schema whose members
    were objects would be answering a different question than "which of these words do I put
    here", and the point is a list a human can read in a refusal.
    """
    for alternative in _alternatives(node, defs):
        members = alternative.get("enum")
        if members is None and "const" in alternative:
            members = [alternative["const"]]
        if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
            continue
        values = list(members)
        if values and all(isinstance(v, (str, int, float, bool)) or v is None for v in values):
            return values
    return None


def closed_object(node: Any, defs: Mapping[str, Any]) -> tuple[list[str], list[str]] | None:
    """``(accepted keys, required keys)`` when ``node`` is a closed object, else ``None``.

    "Closed" means ``additionalProperties: false`` — the property that made refusal (3)
    unhelpful, and the property R13 relies on to keep identity fields out of `ProfileBuckets`.
    An OPEN object is deliberately not described: listing the keys it happens to declare would
    read as an allowlist to an integrator, and it is not one.

    Declaration order is preserved rather than sorted, so the list a refusal prints is the order
    the schema — and therefore the published example — states the fields in.
    """
    for alternative in _alternatives(node, defs):
        if alternative.get("additionalProperties") is not False:
            continue
        properties = alternative.get("properties")
        if not isinstance(properties, Mapping):
            continue
        accepts = [str(key) for key in properties]
        required = alternative.get("required")
        requires = (
            [str(key) for key in required]
            if isinstance(required, Sequence) and not isinstance(required, (str, bytes))
            else []
        )
        return accepts, requires
    return None


@lru_cache(maxsize=8)
def _schema_of(model: type[BaseModel]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """``(schema, $defs)`` for ``model``, built once per process.

    Cached because a refusal is a request path: ``model_json_schema()`` walks every definition
    the model reaches, and doing that per hostile request would hand an attacker a cheaper way to
    spend this agent's CPU than sending a valid solicitation does.
    """
    schema = model.model_json_schema()
    defs = schema.get("$defs")
    return schema, defs if isinstance(defs, Mapping) else {}


def example_pointer(path: str, method: str) -> str:
    """A JSON Pointer (RFC 6901) at this operation's request-body example in the served schema.

    Built from the route's OWN path and the request's own method rather than a literal, so it
    cannot come to name a document location the service does not serve. ``~`` before ``/``: the
    escapes are ordered, and reversing them would double-escape a path containing a tilde.

    The caller passes ``APIRoute.path_format``, not ``APIRoute.path``. They are the same string
    for this door, which has no path parameters — but ``path_format`` is what OpenAPI keys
    ``paths`` by, so a route declared ``/{id:int}`` would appear in the document as ``/{id}`` and
    a pointer built from ``path`` would resolve to nothing.
    """
    escaped = path.replace("~", "~0").replace("/", "~1")
    return f"/paths/{escaped}/{method.lower()}/requestBody/content/application~1json/example"


def validation_detail(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """FastAPI's per-field detail, bounded, and without the ``input`` it normally quotes back.

    ``loc``, ``msg`` and ``type`` only — the three that say WHERE the problem is and WHAT it is.
    Integer segments stay integers, because ``['body','intent','preferences',0,'direction']``
    with a real index is what a caller indexes their own payload with; stringifying it would
    change the shape of a detail this module is otherwise passing through unchanged.
    """
    detail: list[dict[str, Any]] = []
    for error in errors[:MAX_VALIDATION_ERRORS]:
        location = error.get("loc") or ()
        detail.append(
            {
                "type": str(error.get("type", ""))[:MAX_IDENTIFIER_CHARS],
                "loc": [
                    part
                    if isinstance(part, int) and not isinstance(part, bool)
                    else str(part)[:MAX_IDENTIFIER_CHARS]
                    for part in location
                ],
                "msg": str(error.get("msg", ""))[:MAX_VALIDATION_MESSAGE_CHARS],
            }
        )
    return detail


def schema_help(
    model: type[BaseModel],
    errors: Sequence[Mapping[str, Any]],
    *,
    openapi_url: str,
    pointer: str,
) -> dict[str, Any]:
    """The vocabulary the refused caller needed, derived from ``model``'s own schema.

    For each error, two questions are asked of the schema and both are answered where the schema
    has an answer:

    * **at the error's own location** — is this field a closed set of values? That is refusal (2):
      ``direction`` is reported ``missing``, and the field the schema declares there is
      `PreferenceDirection`. A ``missing`` error is the case that makes this worth doing, because
      there is no offending value to look at and the caller has nothing to go on at all.
    * **at its parent** — is the enclosing object closed, and what does it accept? That is
      refusals (1) and (3). Asked for ``missing`` and ``extra_forbidden``, the two error types
      that are about the SET of keys rather than about one value.

    Entries are deduplicated by location, so the seven errors a wholly wrong body produces
    describe the one object they all belong to once instead of seven times.
    """
    schema, defs = _schema_of(model)
    objects: dict[tuple[Any, ...], dict[str, Any]] = {}
    values: dict[tuple[Any, ...], dict[str, Any]] = {}

    for error in errors[:MAX_VALIDATION_ERRORS]:
        location = tuple(error.get("loc") or ())
        if not location or location[0] != BODY_LOC:
            continue
        path = location[1:]

        node = node_at(schema, path, defs)
        if node is not None and path not in values:
            members = permitted_values(node, defs)
            if members is not None:
                values[path] = {"loc": list(location), "values": members}
            # An error pointing AT an object ("Input should be a valid dictionary") is answered
            # by that object's own key set, not by its parent's.
            if path not in objects:
                described = closed_object(node, defs)
                if described is not None:
                    objects[path] = _object_help(location, described)

        if str(error.get("type", "")) in _KEY_SHAPED_ERRORS and path:
            parent = path[:-1]
            if parent not in objects:
                described = closed_object(node_at(schema, parent, defs), defs)
                if described is not None:
                    objects[parent] = _object_help((BODY_LOC, *parent), described)

    # Nothing resolved — an unparseable body, or a shape this walker does not understand. The
    # root object is still the honest answer to "what does this door take", and a refusal that
    # says only "no" is the thing this module exists to stop shipping.
    if not objects and not values:
        described = closed_object(schema, defs)
        if described is not None:
            objects[()] = _object_help((BODY_LOC,), described)

    return {
        "request_schema": str(schema.get("title") or model.__name__),
        "example": {
            "document": openapi_url,
            "pointer": pointer,
            "contract": PACKAGE_CONTRACT,
        },
        "closed_objects": list(objects.values()),
        "permitted_values": list(values.values()),
    }


#: The error types that are about which KEYS an object has rather than about one value's shape.
#: Both are answered by naming the enclosing object's accepted set: ``missing`` says a required
#: key is absent, ``extra_forbidden`` says a key that is present belongs to no declared field.
_KEY_SHAPED_ERRORS = frozenset({"missing", "extra_forbidden"})


def _object_help(location: Sequence[Any], described: tuple[list[str], list[str]]) -> dict[str, Any]:
    """One ``closed_objects`` entry: where the object is, and the keys it takes."""
    accepts, requires = described
    return {"loc": list(location), "accepts": accepts, "requires": requires}


def refusal_body(
    errors: Sequence[Mapping[str, Any]],
    *,
    model: type[BaseModel] | None,
    openapi_url: str,
    pointer: str,
) -> dict[str, Any]:
    """The whole 422 body: the bounded per-field detail, plus help when help can be derived.

    The help is computed inside a `try` and dropped on any failure. Enrichment is an ADDITION to
    a refusal that was already correct, so it may never be the reason a refusal stops happening —
    a walker that raises must cost the caller a less helpful message and nothing else. This is
    the same rule the ``input`` echo broke in the other direction, by being able to turn a 422
    into a 500.
    """
    body: dict[str, Any] = {"detail": validation_detail(errors)}
    if model is None:
        return body
    try:
        body["help"] = schema_help(model, errors, openapi_url=openapi_url, pointer=pointer)
    except Exception:  # pragma: no cover - defence in depth; no known shape reaches it
        _log.warning(
            "could not derive schema help for a refused %s body; answering the plain refusal",
            model.__name__,
            exc_info=True,
        )
    return body


def _body_model(route: APIRoute) -> type[BaseModel] | None:
    """The pydantic model this route validates its body against, or ``None``.

    Read off the route rather than imported, so the help can only ever describe the model that
    actually did the refusing. A route with no body field, or one whose body is not a single
    model, answers ``None`` and gets the plain detail.
    """
    field = getattr(route, "body_field", None)
    if field is None:
        return None
    for attribute in ("type_", "annotation"):
        candidate = getattr(field, attribute, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    info = getattr(field, "field_info", None)
    candidate = getattr(info, "annotation", None)
    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
        return candidate
    return None


class EnrichedRefusalRoute(APIRoute):
    """Answer an unprocessable body with what would have been accepted — and quote none of it.

    Everything this class changes about a 422 is in the BODY. The status is unchanged, the set of
    bodies that reach it is unchanged, and a request that validates never enters the `except` at
    all.

    **There is deliberately no ``except RecursionError`` here**, and that is a measured decision
    rather than an omission. A 4 KB body of 2 000 nested arrays did answer HTTP 500 on this door
    at HEAD — but the captured traceback puts the recursion in ``jsonable_encoder`` walking the
    quoted ``input``, inside FastAPI's own validation-exception handler, and not in the parser. So
    dropping the echo IS the fix, and a second guard would be catching an exception that no longer
    travels. Swept afterwards over 73 distinct nesting depths between 100 and 24 500 (every 100 up
    to 2 900, then every 500), in both array and object shapes, for 146 probes: 422 while pydantic
    can read the document, 400 — FastAPI's own "There was an error parsing the body" — from depth
    10 000, and no 500 at any depth in the sweep. `buyer_svc.intent.routes._BoundedBodyRoute` DOES
    carry that
    clause, for a fault measured on ITS door; it was not reproduced on this one, and copying it
    over would have shipped an unreachable branch claiming to close a fault this class had
    already closed by other means.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        model = _body_model(self)
        # `path_format`, not `path`: it is the key OpenAPI files `paths` under.
        path = getattr(self, "path_format", None) or self.path

        async def refuse_helpfully(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                openapi_url = getattr(request.app, "openapi_url", None) or DEFAULT_OPENAPI_URL
                return JSONResponse(
                    status_code=HTTP_UNPROCESSABLE,
                    content=refusal_body(
                        exc.errors(),
                        model=model,
                        openapi_url=str(openapi_url),
                        pointer=example_pointer(path, request.method),
                    ),
                )

        return refuse_helpfully
