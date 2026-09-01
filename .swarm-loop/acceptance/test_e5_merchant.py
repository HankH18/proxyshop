"""Epic E5 — Merchant: install, pixel collection, discount codes, onboarding envelope.

FROZEN ACCEPTANCE SUITE. These ten tests encode the merchant-side half of the SPEC:

  * R6 / C5  — installing the Shopify app registers the web pixel and the order
               webhooks, and requests no protected-customer-data scope (T-050).
  * R4 / R5  — the pixel collector accepts join keys only, never PII, and makes a
               dropped pixel event a *visible gap* rather than a silent default (T-051).
  * R3 / A5  — an accepted offer becomes a single-use, short-expiry discount code with
               a permalink; a conflicting `combinesWith` configuration is caught before
               any permalink is handed out; a duplicate redemption is an offer-integrity
               ledger event, not a crash (T-052).
  * R6 / R9  — the plain-language onboarding interview yields the golden envelope with
               `activation == "shadow"`, activation requires a recorded written approval
               artifact bound to the approved envelope, and envelope edits create new
               versions while old versions stay immutable (T-053).

Not covered here, by construction: T-054's dashboard is TypeScript and structurally
uncoverable by a pytest suite, and genuine Shopify API conformance is unprovable
offline (SPEC A4). `e5_merchant_passing == 10` therefore does NOT mean the dashboard
was verified.

Authoring rules (see README.md in this directory): every product import happens INSIDE
a test function, and every test carries an `epic` and a `ticket` marker. Nothing here
needs a network socket, a database, a container, or a reading of the wall clock: the
one test that bounds a discount code's validity window injects the reference instant
`_T_NOW` into `create_code(...)` instead of asking the machine what time it is.
"""
from __future__ import annotations

import copy
import json
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_MISSING = object()

# The single reference instant this file ever uses. It is a constant, never
# `datetime.now()`: the suite must produce the same verdict on any machine at any
# moment. It is injected into the product call that needs a "now" (`create_code`),
# so an implementation that derives a validity window from it is checked against the
# very instant it was handed.
_T_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------
# Generic helpers over product objects whose exact class we deliberately do not pin.
# --------------------------------------------------------------------------------------
def _plain(value, _depth: int = 0):
    """Best-effort conversion of an arbitrary product object into JSON-able data."""
    if _depth > 12:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v, _depth + 1) for v in value]
    for attr in ("model_dump", "dict", "_asdict", "to_dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return _plain(fn(), _depth + 1)
            except Exception:  # pragma: no cover - defensive
                pass
    if hasattr(value, "__dict__") and vars(value):
        return {
            str(k): _plain(v, _depth + 1)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def _dump(value) -> str:
    """Stable text rendering of an arbitrary product object, for substring assertions."""
    return json.dumps(_plain(value), sort_keys=True, default=str)


def _field(obj, *names):
    """First present attribute/key among `names`, or `_MISSING`."""
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    plain = _plain(obj)
    if isinstance(plain, dict):
        for name in names:
            if name in plain:
                return plain[name]
    return _MISSING


def _outcome(fn, *args, **kwargs):
    """Call `fn`; classify the result as ("accepted", value) or ("rejected", detail).

    Any exception counts as a rejection. That is only safe because every test using
    this helper also asserts a positive control on the same callable — a wrong
    signature or a missing module fails the control, so it can never masquerade as a
    clean rejection.
    """
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        return "rejected", exc
    if result is None or result is False:
        return "rejected", result
    for key in ("accepted", "ok", "valid", "allowed"):
        value = _field(result, key)
        if value is not _MISSING and value is False:
            return "rejected", result
    for key in ("rejected", "refused", "error", "errors", "violations"):
        value = _field(result, key)
        if value is not _MISSING and value:
            return "rejected", result
    return "accepted", result


def _signal(value, words) -> bool:
    """True when `value` carries a *truthy* marker named by one of `words`.

    Deliberately not a substring test over the serialized object: a record with a
    permanently-present `"gaps": []` field must read as "no gap", not as "gap".
    """

    def walk(node):
        if isinstance(node, dict):
            for key, sub in node.items():
                if sub and any(word in str(key).lower() for word in words):
                    return True
                if walk(sub):
                    return True
            return False
        if isinstance(node, list):
            return any(walk(item) for item in node)
        if isinstance(node, str):
            return any(word in node.lower() for word in words)
        return False

    return walk(_plain(value))


def _parse_ts(text: str):
    cleaned = str(text).strip().strip('"').replace("Z", "+00:00").replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class _AutoDict(dict):
    """A mapping that answers *any* key, so a product call site cannot KeyError on us.

    Seeded so that whichever path the implementation walks to read shop configuration,
    it finds the same seeded values (e.g. a conflicting `combinesWith` block).
    """

    def __init__(self, seed=None, **initial):
        super().__init__(**initial)
        self._seed = dict(seed or {})
        for key, value in self._seed.items():
            self.setdefault(key, value)

    def __missing__(self, key):
        child = _AutoDict(self._seed)
        dict.__setitem__(self, key, child)
        return child

    def get(self, key, default=None):  # noqa: D102 - tolerant on purpose
        return self[key]


class _RecordingAdminClient:
    """In-process stand-in for the Shopify Admin GraphQL client.

    Records every interaction whatever the call shape: a generic `execute(...)` /
    `query(...)` / `mutate(...)`, or any convenience method the implementation prefers.
    Returns an auto-vivifying response so no attribute or key lookup on the reply
    raises.
    """

    def __init__(self, seed=None):
        self.calls = []  # list of (name, args, kwargs)
        self._seed = dict(seed or {})
        self._seed.setdefault("userErrors", [])

    def _record(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))
        return _AutoDict(self._seed, data=_AutoDict(self._seed))

    def execute(self, *args, **kwargs):
        return self._record("execute", args, kwargs)

    query = execute
    mutate = execute
    request = execute

    def __call__(self, *args, **kwargs):
        return self._record("__call__", args, kwargs)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _recorder(*args, **kwargs):
            return self._record(name, args, kwargs)

        return _recorder

    def transcript(self) -> str:
        return "\n".join(
            json.dumps(
                {"call": name, "args": _plain(args), "kwargs": _plain(kwargs)},
                sort_keys=True,
                default=str,
            )
            for name, args, kwargs in self.calls
        )

    def calls_mentioning(self, needle: str):
        needle = needle.lower().replace("_", "")
        out = []
        for name, args, kwargs in self.calls:
            text = json.dumps(
                {"call": name, "args": _plain(args), "kwargs": _plain(kwargs)},
                sort_keys=True,
                default=str,
            )
            if needle in text.lower().replace("_", ""):
                out.append(text)
        return out


_TOPIC_RE = re.compile(r"\b([A-Za-z]{3,20})[/_]([A-Za-z_]{3,30})\b")

REQUIRED_WEBHOOK_TOPICS = frozenset({"orders/paid", "orders/fulfilled", "refunds/create"})

# Order/checkout lifecycle topics an install could plausibly subscribe. The install is
# required to subscribe exactly the three required ones out of this family: subscribing
# `checkouts/*` would make a second checkout-observation path, which C5 forbids, and
# subscribing extra order topics is scope the SPEC does not ask for. Topics outside this
# family (e.g. `app/uninstalled`, Shopify's mandatory compliance webhooks) are ignored.
LIFECYCLE_WEBHOOK_TOPICS = frozenset(
    REQUIRED_WEBHOOK_TOPICS
    | {
        "orders/create",
        "orders/updated",
        "orders/delete",
        "orders/edited",
        "orders/cancelled",
        "orders/canceled",
        "orders/partially_fulfilled",
        "orders/risk_assessment_changed",
        "refunds/update",
        "fulfillments/create",
        "fulfillments/update",
        "checkouts/create",
        "checkouts/update",
        "checkouts/delete",
        "carts/create",
        "carts/update",
    }
)


def _topics_in(text: str) -> set:
    """Canonical `resource/action` webhook topics mentioned anywhere in `text`."""
    found = set()
    for head, tail in _TOPIC_RE.findall(text):
        found.add(f"{head.lower()}/{tail.lower()}")
    return found


def _interview_fixture():
    """Load T-053's interview fixture: (transcript, golden_envelope).

    Convention (frozen, and quoted into T-053's task packet): a JSON file under
    `fixtures/interviews/` carrying both the interview transcript and the golden
    envelope it must produce, under keys `transcript` and `expected_envelope`. A
    sibling-file pair `<name>.transcript.json` + `<name>.envelope.json` is also
    accepted.
    """
    root = REPO_ROOT / "fixtures" / "interviews"
    assert root.is_dir(), (
        "T-053 must ship an interview fixture directory at fixtures/interviews/ "
        "containing the transcript and the golden envelope it produces"
    )

    transcript_keys = ("transcript", "dialogue", "messages", "turns")
    golden_keys = ("expected_envelope", "golden_envelope", "envelope")

    for path in sorted(root.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        transcript = next(
            (data[k] for k in transcript_keys if k in data and data[k]), None
        )
        golden = next((data[k] for k in golden_keys if k in data and data[k]), None)
        if transcript is not None and isinstance(golden, dict):
            return transcript, golden

    for path in sorted(root.rglob("*.transcript.json")):
        sibling = path.with_name(path.name.replace(".transcript.json", ".envelope.json"))
        if sibling.exists():
            transcript = json.loads(path.read_text())
            golden = json.loads(sibling.read_text())
            if isinstance(golden, dict):
                return transcript, golden

    raise AssertionError(
        "no interview fixture found under fixtures/interviews/: expected a JSON file "
        "with 'transcript' and 'expected_envelope' keys, or a "
        "<name>.transcript.json / <name>.envelope.json pair"
    )


def _envelope_from_fixture():
    """The Envelope the product builds from T-053's fixture transcript."""
    from apps.merchant.svc.src.onboarding import envelope_from_transcript

    transcript, golden = _interview_fixture()
    return envelope_from_transcript(transcript), golden


def _offer(code_hint: str = "acceptance-offer-1"):
    """A minimal accepted offer, in the shape DESIGN §Interfaces gives POST /codes."""
    return {
        "offer_id": code_hint,
        "auction_id": "auc-acceptance-1",
        "bid_ref": "bid-acceptance-1",
        "product_ref": "gid://shopify/ProductVariant/1001",
        "variant_id": "gid://shopify/ProductVariant/1001",
        "quantity": 1,
        "list_price": "100.00",
        "unit_price": "90.00",
        "currency": "USD",
        "discount_pct": 10.0,
        "discount_type": "percentage",
        "checkout_url": "https://acceptance-store.myshopify.com/cart",
    }


# --------------------------------------------------------------------------------------
# T-050 — install
# --------------------------------------------------------------------------------------
@pytest.mark.epic("E5")
@pytest.mark.ticket("T-050")
def test_install_registers_the_pixel_and_the_required_webhook_topics():
    """R6/C5: install registers the web pixel and exactly the three order webhooks."""
    from apps.merchant.svc.src.install import install

    client = _RecordingAdminClient()
    install("acceptance-store.myshopify.com", client)

    assert client.calls, "install() made no calls against the injected admin client"

    pixel_calls = client.calls_mentioning("webPixelCreate")
    assert pixel_calls, (
        "install() never called webPixelCreate; recorded transcript:\n"
        + client.transcript()
    )

    pixel_text = "\n".join(pixel_calls)
    tails = [
        match.group(1).lstrip()
        for match in re.finditer(r"settings\\?\"?\s*[:=]\s*(.{0,60})", pixel_text, re.I)
    ]
    assert tails, "the webPixelCreate call carries no settings payload"
    empty = re.compile(r"(\{\s*\}|\\?\"\\?\"|''|null|None|\[\s*\])")
    assert any(not empty.match(tail) for tail in tails), (
        "webPixelCreate was called with an empty settings payload: " + repr(tails)
    )

    topics = _topics_in(client.transcript()) & LIFECYCLE_WEBHOOK_TOPICS
    assert topics == set(REQUIRED_WEBHOOK_TOPICS), (
        "install() must subscribe exactly orders/paid, orders/fulfilled and "
        f"refunds/create; order-lifecycle topics actually seen: {sorted(topics)}"
    )


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-050")
def test_no_protected_customer_data_scope_is_requested():
    """C5: the requested scope list holds read_orders and no protected-customer scope."""
    from apps.merchant.svc.src.install import REQUIRED_SCOPES

    scopes = {str(s).strip().lower() for s in REQUIRED_SCOPES}
    assert scopes, "REQUIRED_SCOPES is empty"

    assert "read_orders" in scopes, (
        f"read_orders is required for order reconciliation; got {sorted(scopes)}"
    )

    protected = {
        "read_customers",
        "write_customers",
        "read_all_orders",
        "read_customer_payment_methods",
        "read_customer_merge",
        "write_customer_merge",
        "read_customer_events",
        "read_marketing_events",
    }
    offending = scopes & protected
    assert not offending, (
        "C5 forbids protected-customer-data scopes; requested: " + repr(sorted(offending))
    )


# --------------------------------------------------------------------------------------
# T-051 — pixel collector
# --------------------------------------------------------------------------------------
_CLEAN_PIXEL_EVENT = {
    "clientId": "acceptance-client-1",
    "checkoutToken": "acceptance-checkout-token-1",
    "orderId": "gid://shopify/Order/1001",
    "discountApplications": [{"code": "PSHOP-ACC-1", "value": 10.0, "type": "code"}],
}


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-051")
def test_collector_rejects_every_pii_field():
    """R5/C5: the collector schema accepts join keys only and rejects every PII field."""
    from apps.merchant.svc.src.collector import accept_pixel_event

    verdict, detail = _outcome(accept_pixel_event, dict(_CLEAN_PIXEL_EVENT))
    assert verdict == "accepted", (
        "the join-key-only payload must be accepted; collector said: " + repr(detail)
    )

    pii_variants = {
        "email": "shopper@example.com",
        "phone": "+15555550100",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "address": "1 Analytical Engine Way, London",
        "customer_id": "gid://shopify/Customer/42",
        "customerId": "gid://shopify/Customer/42",
    }
    for field, value in pii_variants.items():
        payload = dict(_CLEAN_PIXEL_EVENT)
        payload[field] = value
        verdict, detail = _outcome(accept_pixel_event, payload)
        assert verdict == "rejected", (
            f"payload carrying PII field {field!r} was accepted by the collector; "
            f"result: {_dump(detail)}"
        )
        if verdict == "rejected" and not isinstance(detail, Exception):
            assert value not in _dump(detail), (
                f"the rejected record still carries the {field!r} value"
            )


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-051")
def test_pixel_drop_is_tolerated_and_leaves_a_visible_gap():
    """R4: a dropped pixel event neither crashes the collector nor defaults silently."""
    from apps.merchant.svc.src.collector import accept_pixel_event

    gap_words = ("gap", "missing", "incomplete", "dropped", "unresolved")

    complete = accept_pixel_event(dict(_CLEAN_PIXEL_EVENT))
    assert not _signal(complete, gap_words), (
        "a complete pixel event must not be marked as a gap: " + _dump(complete)
    )

    dropped = dict(_CLEAN_PIXEL_EVENT)
    dropped["orderId"] = None
    dropped["discountApplications"] = None

    try:
        record = accept_pixel_event(dropped)
    except Exception as exc:  # noqa: BLE001 - the whole point is that it must not raise
        raise AssertionError(
            "a dropped/partial pixel event must not raise: " + repr(exc)
        ) from exc

    assert _signal(record, gap_words), (
        "a dropped pixel event must leave an explicit, visible gap marker "
        f"(one of {gap_words}); the collector produced: {_dump(record)}"
    )


# --------------------------------------------------------------------------------------
# T-052 — discount codes
# --------------------------------------------------------------------------------------
_COMBINES_OK = {
    "combinesWith": {
        "orderDiscounts": True,
        "productDiscounts": True,
        "shippingDiscounts": True,
    }
}
_COMBINES_CONFLICT = {
    "combinesWith": {
        "orderDiscounts": False,
        "productDiscounts": False,
        "shippingDiscounts": False,
    },
    "hasActiveAutomaticDiscount": True,
    "automaticDiscounts": [{"id": "gid://shopify/DiscountAutomaticNode/9", "title": "sitewide"}],
}


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-052")
def test_code_is_single_use_with_a_bounded_expiry():
    """R3/C5: the created discount code is usageLimit 1, expires within 48h, permalinked.

    The reference instant is injected (`now=_T_NOW`), never read from the machine's
    clock: `create_code` must anchor the code's validity window at the instant it is
    given, so this test's verdict is the same on every machine at every moment.
    """
    from apps.merchant.svc.src.codes import create_code

    client = _RecordingAdminClient(seed=_COMBINES_OK)
    result = create_code("acceptance-store", _offer(), client, now=_T_NOW)

    mutation_calls = client.calls_mentioning("discountCodeBasicCreate")
    assert mutation_calls, (
        "create_code() never issued discountCodeBasicCreate; transcript:\n"
        + client.transcript()
    )
    text = "\n".join(mutation_calls)

    usage = re.search(r"usage[_]?limit\\?\"?\s*[:=]\s*\"?(\d+)", text, re.I)
    assert usage, "the discountCodeBasicCreate arguments declare no usageLimit"
    assert int(usage.group(1)) == 1, (
        "A5 requires a single-use code; usageLimit was " + usage.group(1)
    )

    ends = re.search(
        r"(?:ends[_]?at|expires[_]?at|expiry|expires)\\?\"?\s*[:=]\s*\\?\"?"
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.+\-]{4,40}Z?)",
        text,
        re.I,
    )
    assert ends, "the discountCodeBasicCreate arguments declare no expiry timestamp"
    ends_at = _parse_ts(ends.group(1))
    assert ends_at is not None, "the expiry timestamp is not ISO-8601: " + ends.group(1)

    starts = re.search(
        r"starts[_]?at\\?\"?\s*[:=]\s*\\?\"?"
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.+\-]{4,40}Z?)",
        text,
        re.I,
    )
    reference = _parse_ts(starts.group(1)) if starts else _T_NOW
    if reference is None:
        reference = _T_NOW

    lifetime = ends_at - reference
    assert timedelta(0) < lifetime <= timedelta(hours=48), (
        "DESIGN pins a short expiry (<= 48h); measured from "
        + reference.isoformat()
        + (
            " (the code's own startsAt)"
            if starts
            else " (the injected `now`, which create_code must anchor the window at "
            "when it declares no startsAt)"
        )
        + ", the code's lifetime was "
        + str(lifetime)
    )

    code = _field(result, "code", "discount_code", "discountCode")
    assert isinstance(code, str) and code.strip(), (
        "create_code() must return the created code; got " + repr(code)
    )
    permalink = _field(result, "permalink_url", "permalink", "permalinkUrl", "url")
    assert isinstance(permalink, str) and permalink.startswith("http"), (
        "create_code() must return a cart permalink URL; got " + repr(permalink)
    )
    assert code in permalink, (
        f"the permalink {permalink!r} does not pre-apply the created code {code!r}"
    )
    assert code in text, "the created code was never sent to discountCodeBasicCreate"


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-052")
def test_conflicting_combines_with_is_detected_before_redirect():
    """R3: a combinesWith conflict is repriced or refused before any permalink exists."""
    from apps.merchant.svc.src.codes import create_code

    requested_pct = _offer()["discount_pct"]

    # Positive control: with a permissive shop configuration the offer goes through
    # untouched. Without this, "always reprice to zero" would satisfy the test below.
    ok_client = _RecordingAdminClient(seed=_COMBINES_OK)
    ok_result = create_code("acceptance-store", _offer("ok-offer"), ok_client)
    ok_permalink = _field(ok_result, "permalink_url", "permalink", "permalinkUrl", "url")
    assert isinstance(ok_permalink, str) and ok_permalink.startswith("http"), (
        "control case: a non-conflicting offer must still yield a permalink; got "
        + repr(ok_permalink)
    )
    ok_applied = _field(ok_result, "discount_pct", "discountPct", "applied_discount_pct")
    if ok_applied is not _MISSING:
        assert ok_applied == requested_pct, (
            "control case: a non-conflicting offer must keep its requested discount; "
            f"asked for {requested_pct!r}, applied {ok_applied!r}"
        )

    bad_client = _RecordingAdminClient(seed=_COMBINES_CONFLICT)
    verdict, detail = _outcome(
        create_code, "acceptance-store", _offer("conflicting-offer"), bad_client
    )

    if verdict == "rejected":
        return

    permalink = _field(detail, "permalink_url", "permalink", "permalinkUrl", "url")
    applied = _field(detail, "discount_pct", "discountPct", "applied_discount_pct")
    repriced = applied is not _MISSING and applied != requested_pct

    assert repriced, (
        "a discount conflicting with the shop's combinesWith settings must be refused "
        "or re-priced before a permalink is returned; create_code returned permalink "
        f"{permalink!r} with the originally requested discount {requested_pct!r}"
    )


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-052")
def test_duplicate_redemption_emits_an_integrity_event_not_an_exception():
    """A5/R3: a second redemption of a single-use code is an integrity event, not a crash."""
    from apps.merchant.svc.src.codes import create_code, on_redemption

    client = _RecordingAdminClient(seed=_COMBINES_OK)
    created = create_code("acceptance-store", _offer(), client)
    code = _field(created, "code", "discount_code", "discountCode")
    assert isinstance(code, str) and code.strip(), (
        "create_code() must return the created code; got " + repr(code)
    )

    integrity_words = ("integrity", "duplicate", "reuse", "reused", "already", "double")

    first = on_redemption(code, {"id": "gid://shopify/Order/2001", "discountCodes": [code]})
    assert not _signal(first, integrity_words), (
        "the first redemption must not be flagged as an integrity violation: "
        + _dump(first)
    )

    second = on_redemption(code, {"id": "gid://shopify/Order/2002", "discountCodes": [code]})
    second_text = _dump(second).lower()

    assert _signal(second, integrity_words), (
        "a duplicate redemption must surface as an offer-integrity event; "
        "on_redemption returned: " + second_text
    )
    assert "kind" in second_text, (
        "the duplicate-redemption result must carry a LedgerEvent (with a `kind`); got: "
        + second_text
    )


# --------------------------------------------------------------------------------------
# T-053 — onboarding interview, approval, envelope versions
# --------------------------------------------------------------------------------------
_ENVELOPE_KEYS = (
    "store_id",
    "version",
    "floors",
    "max_discount_pct",
    "budget_cap",
    "pursue_clusters",
    "standing_commitments",
    "activation",
)


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-053")
def test_interview_transcript_yields_the_expected_envelope_version():
    """R6: the fixture transcript produces the golden Envelope, defaulting to shadow."""
    from apps.merchant.svc.src.onboarding import envelope_from_transcript

    transcript, golden = _interview_fixture()

    missing = [key for key in _ENVELOPE_KEYS if key not in golden]
    assert not missing, (
        "the golden envelope in fixtures/interviews/ is not a DESIGN Envelope; "
        f"missing keys: {missing}"
    )
    assert golden["activation"] == "shadow", (
        "R7: a freshly interviewed envelope defaults to shadow, not "
        + repr(golden["activation"])
    )

    produced = _plain(envelope_from_transcript(transcript))
    assert isinstance(produced, dict), (
        "envelope_from_transcript must return an Envelope, got " + repr(produced)
    )

    for key, expected in golden.items():
        assert key in produced, f"produced envelope is missing golden key {key!r}"
        assert produced[key] == expected, (
            f"envelope field {key!r}: expected {expected!r}, produced {produced[key]!r}"
        )

    again = _plain(envelope_from_transcript(transcript))
    assert again == produced, "envelope_from_transcript is not deterministic"


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-053")
def test_activation_requires_a_recorded_written_approval_artifact():
    """R6: activation is refused without an approval artifact bound to that envelope."""
    from apps.merchant.svc.src.onboarding import activate, approval_digest

    envelope, _golden = _envelope_from_fixture()
    digest = approval_digest(envelope)
    assert isinstance(digest, str) and digest.strip(), (
        "approval_digest must return the hash of the approved envelope text; got "
        + repr(digest)
    )

    good = {
        "approver": "Acceptance Merchant Owner",
        "approved_at": "2026-01-01T00:00:00+00:00",
        "envelope_hash": digest,
    }

    # The negative cases run first, on their own untouched copy, so that an
    # implementation which activates in place cannot leak state between them.
    other_envelope = copy.deepcopy(envelope)
    try:
        other_digest = approval_digest(
            dict(_plain(envelope), max_discount_pct=99, store_id="some-other-store")
        )
    except Exception:
        other_digest = digest[::-1]

    bad_approvals = {
        "no approval artifact at all": None,
        "approval with no approver": {
            "approved_at": good["approved_at"],
            "envelope_hash": digest,
        },
        "approval with no timestamp": {
            "approver": good["approver"],
            "envelope_hash": digest,
        },
        "approval whose hash is of a different envelope": {
            "approver": good["approver"],
            "approved_at": good["approved_at"],
            "envelope_hash": other_digest,
        },
    }
    for label, approval in bad_approvals.items():
        verdict, detail = _outcome(activate, copy.deepcopy(other_envelope), approval)
        if verdict == "accepted":
            assert _field(detail, "activation") != "active", (
                f"activation succeeded with {label}: " + _dump(detail)
            )

    # Positive control last: a complete, envelope-bound written approval activates.
    verdict, activated = _outcome(activate, envelope, good)
    assert verdict == "accepted", (
        "a complete written approval must activate the envelope; activate said: "
        + repr(activated)
    )
    assert _field(activated, "activation") == "active", (
        "activation must flip the envelope to active; got "
        + repr(_field(activated, "activation"))
    )


@pytest.mark.epic("E5")
@pytest.mark.ticket("T-053")
def test_envelope_edits_create_new_versions_and_old_versions_are_immutable():
    """R6/R9: editing an envelope creates a new version; prior versions never mutate."""
    from apps.merchant.svc.src.onboarding import edit

    v1, _golden = _envelope_from_fixture()
    v1_before = _dump(v1)
    v1_version = _field(v1, "version")
    assert isinstance(v1_version, int), (
        "an Envelope carries an integer version; got " + repr(v1_version)
    )

    current_pct = _field(v1, "max_discount_pct")
    new_pct = 2 if current_pct == 1 else 1

    v2 = edit(v1, {"max_discount_pct": new_pct})
    v2_version = _field(v2, "version")
    assert isinstance(v2_version, int) and v2_version > v1_version, (
        f"edit() must return a new version number; {v1_version!r} -> {v2_version!r}"
    )
    assert _field(v2, "max_discount_pct") == new_pct, (
        "edit() did not apply the requested change; max_discount_pct is "
        + repr(_field(v2, "max_discount_pct"))
    )
    assert _dump(v1) == v1_before, (
        "edit() mutated the prior envelope version in place:\n"
        f"before: {v1_before}\nafter:  {_dump(v1)}"
    )

    v2_before = _dump(v2)
    third_pct = 3 if new_pct != 3 else 4
    v3 = edit(v2, {"max_discount_pct": third_pct})
    assert _field(v3, "version") > v2_version, (
        "a second edit must produce a third version; got " + repr(_field(v3, "version"))
    )
    assert _dump(v2) == v2_before, "the second envelope version was mutated by a later edit"
    assert _dump(v1) == v1_before, "the first envelope version was mutated by a later edit"
