"""Epic E7 — Buyer: clarification, pseudonymity, provenance labels, accept, feedback.

Covers the SPEC surface that E7 owns:

* **R1** — a shopping need becomes a *confirmed structured intent* after **at most three**
  clarifying questions, and **no auction exists before the buyer confirms**.
* **R5** — stores never receive buyer identity: the `BuyerProfile` handed out carries only
  a pseudonym plus coarsened buckets, and the pseudonym rotates per session and is never
  reissued.
* **R2** — the Python half of shortlist provenance labelling: `Provenance.source` maps to
  "store-confirmed" vs "from their website", and a seller-asserted claim gets neither.
* **R3** — accept hands off to the *exchange's* permalink; the buyer never mints a
  checkout URL of its own.
* **R14** — exactly one structured feedback prompt, offered only for network-routed
  orders, with no free-text field, and a submission that lands as a `feedback`
  `LedgerEvent`.

The TypeScript halves of R2/R3/R14 (`apps/buyer/app/**`) are structurally uncoverable by a
pytest suite; they are carried by `build_succeeds` and the tickets' own vitest runs.

Authoring rules (see `.swarm-loop/acceptance/README.md`): every product import happens
INSIDE the test body, and every test carries `epic` + `ticket` markers. Module scope holds
stdlib and pytest only.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Scripted buyer dialogues + their golden intents. T-071 owns `fixtures/dialogues/**`.
#: Each file is JSON: {"turns": [str, ...],            # buyer utterances, first = opener
#:                     "llm_script": [str, ...],       # optional recorded LLM replies
#:                     "expected_intent": {...}}       # the golden Intent for this dialogue
DIALOGUE_DIR = REPO_ROOT / "fixtures" / "dialogues"

#: Keys that must never appear anywhere inside a `BuyerProfile` (R5).
IDENTITY_KEY_RE = re.compile(
    r"e[-_ ]?mail|phone|first[_ -]?name|last[_ -]?name|full[_ -]?name|"
    r"given[_ -]?name|surname|address|street|postal|zip[_ -]?code|"
    r"account[_ -]?id|customer[_ -]?id|buyer[_ -]?id|user[_ -]?id|"
    r"identity|ip[_ -]?address|device[_ -]?id",
    re.IGNORECASE,
)

#: DESIGN §Data models pins `BuyerProfile.buckets` to exactly these five keys.
PROFILE_BUCKET_KEYS = {
    "budget_band",
    "category_affinity",
    "frequency_tier",
    "region",
    "first_time",
}

#: DESIGN §Interfaces pins `Intent.hard_constraints[].op` / `preferences[].direction`.
CONSTRAINT_OPS = {"eq", "lte", "gte", "in", "contains"}
PREFERENCE_DIRECTIONS = {"maximize", "minimize", "prefer"}


# --- tiny shape helpers (touch no product code) ------------------------------------
_MISSING = object()


def _get(obj, key: str, default=_MISSING):
    """Read `key` off a mapping or a model object."""
    try:
        return obj[key]  # type: ignore[index]
    except Exception:
        pass
    if hasattr(obj, key):
        return getattr(obj, key)
    if default is _MISSING:
        raise AssertionError(f"expected {key!r} on {obj!r}")
    return default


def _plain(obj, _depth: int = 0):
    """Recursively reduce a model / dataclass / mapping to plain JSON-ish data."""
    if _depth > 12 or obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    for attr in ("model_dump", "dict", "to_dict", "_asdict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                obj = fn()
                break
            except Exception:
                continue
    if isinstance(obj, dict):
        return {str(k): _plain(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v, _depth + 1) for v in obj]
    own = getattr(obj, "__dict__", None)
    if isinstance(own, dict) and own:
        return {
            str(k): _plain(v, _depth + 1)
            for k, v in own.items()
            if not str(k).startswith("_")
        }
    return str(obj)


def _walk(value, path: str = ""):
    """Yield every (key, value) pair inside a plain nested structure."""
    if isinstance(value, dict):
        for key, sub in value.items():
            here = f"{path}.{key}" if path else str(key)
            yield str(key), sub
            yield from _walk(sub, here)
    elif isinstance(value, (list, tuple)):
        for index, sub in enumerate(value):
            yield from _walk(sub, f"{path}[{index}]")


def _canon(value):
    """Order- and numeric-type-insensitive canonical form, for golden comparison."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(
            (_canon(v) for v in value),
            key=lambda v: json.dumps(v, sort_keys=True, default=str),
        )
    return value


def _blob(value) -> str:
    return json.dumps(_plain(value), sort_keys=True, default=str)


class _ScriptedLLM:
    """A deterministic, protocol-agnostic LLM double.

    Whatever method the buyer agent reaches for, it gets the next recorded reply from the
    fixture's `llm_script`. Being permissive about the *method name* keeps the frozen goal
    from pinning an LLM client protocol nobody has written down yet; being scripted keeps
    the run deterministic and offline (C9/D3).
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.cursor = 0

    def _reply(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.cursor < len(self.script):
            reply = self.script[self.cursor]
            self.cursor += 1
            return reply
        return self.script[-1] if self.script else ""

    def __call__(self, *args, **kwargs):
        return self._reply(*args, **kwargs)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._reply


class _Recorder:
    """A protocol-agnostic recording client. Every call lands in `.calls`."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def _bind(self, name):
        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.result

        return _call

    def __call__(self, *args, **kwargs):
        return self._bind("__call__")(*args, **kwargs)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._bind(name)


class _Sink:
    """A protocol-agnostic event sink; every emitted object lands in `.events`."""

    def __init__(self):
        self.events = []
        self.calls = []

    def _bind(self, name):
        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            self.events.extend(args)
            self.events.extend(kwargs.values())
            return None

        return _call

    def __call__(self, *args, **kwargs):
        return self._bind("__call__")(*args, **kwargs)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._bind(name)


def _dialogue_fixtures():
    """Load T-071's scripted dialogue fixtures; they are the ground truth for R1."""
    assert DIALOGUE_DIR.is_dir(), (
        f"R1 golden dialogues are missing: {DIALOGUE_DIR} does not exist. T-071 owns "
        f"fixtures/dialogues/** and must ship scripted dialogues with their expected Intents."
    )
    files = sorted(DIALOGUE_DIR.glob("*.json"))
    assert len(files) >= 3, (
        f"expected at least 3 scripted dialogue fixtures in {DIALOGUE_DIR}, found "
        f"{len(files)} — one of them a maximally vague opener"
    )
    loaded = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        turns = data.get("turns")
        assert isinstance(turns, list) and turns and all(
            isinstance(t, str) and t.strip() for t in turns
        ), f"{path.name}: 'turns' must be a non-empty list of buyer utterances"
        loaded.append((path.name, data))
    return loaded


# ===================================================================================
# T-071 — R1: at most three clarifying questions, then a confirmed structured intent
# ===================================================================================


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-071")
def test_clarification_loop_asks_at_most_three_questions():
    """R1: the clarification loop asks at most 3 questions before producing an intent."""
    from apps.buyer.svc.src.intent import clarify

    fixtures = _dialogue_fixtures()
    asked = []
    for name, data in fixtures:
        outcome = clarify(data["turns"], _ScriptedLLM(data.get("llm_script", [])))
        questions = _get(outcome, "questions")
        assert isinstance(questions, (list, tuple)), (
            f"{name}: clarify(...) must report the questions it asked as a sequence, "
            f"got {type(questions).__name__}"
        )
        assert all(isinstance(q, str) and q.strip() for q in questions), (
            f"{name}: every clarifying question must be non-empty text"
        )
        assert len(questions) <= 3, (
            f"{name}: R1 caps the clarification loop at 3 questions, asked {len(questions)}: "
            f"{list(questions)!r}"
        )
        assert _get(outcome, "intent") is not None, (
            f"{name}: the loop must terminate in a structured Intent, not None"
        )
        asked.append(len(questions))

    assert max(asked) >= 1, (
        "no scripted dialogue caused a single clarifying question — the fixture set does "
        "not exercise the clarification loop at all, so the <=3 cap is untested"
    )


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-071")
def test_confirmed_intent_is_structured_with_use_case_constraints_and_budget_band():
    """R1/R19: the produced Intent matches the golden and separates filters from scores."""
    from apps.buyer.svc.src.intent import clarify

    required = {"query", "hard_constraints", "preferences", "budget_band"}
    saw_a_constraint = False
    saw_a_preference = False

    for name, data in _dialogue_fixtures():
        expected = data.get("expected_intent")
        assert isinstance(expected, dict), f"{name}: 'expected_intent' must be an object"
        assert required.issubset(expected), (
            f"{name}: the golden intent must pin at least {sorted(required)}, "
            f"has {sorted(expected)}"
        )

        outcome = clarify(data["turns"], _ScriptedLLM(data.get("llm_script", [])))
        intent = _plain(_get(outcome, "intent"))
        assert isinstance(intent, dict), f"{name}: the Intent must serialize to an object"

        assert str(intent.get("query") or "").strip(), (
            f"{name}: R1 requires a non-empty use case / query on the confirmed intent"
        )
        assert str(intent.get("budget_band") or "").strip(), (
            f"{name}: R1 requires a budget band on the confirmed intent"
        )

        constraints = intent.get("hard_constraints")
        preferences = intent.get("preferences")
        assert isinstance(constraints, list), f"{name}: hard_constraints must be a list"
        assert isinstance(preferences, list), f"{name}: preferences must be a list"

        for item in constraints:
            item = _plain(item)
            assert isinstance(item, dict), f"{name}: hard constraint must be an object"
            assert str(item.get("field") or "").strip(), f"{name}: hard constraint needs a field"
            assert item.get("op") in CONSTRAINT_OPS, (
                f"{name}: R19 hard constraints are filters — op must be one of "
                f"{sorted(CONSTRAINT_OPS)}, got {item.get('op')!r}"
            )
            assert item.get("weight") is None, (
                f"{name}: R19 — a hard constraint is a filter and carries no weight, got "
                f"{item.get('weight')!r}"
            )
            saw_a_constraint = True

        for item in preferences:
            item = _plain(item)
            assert isinstance(item, dict), f"{name}: preference must be an object"
            assert str(item.get("field") or "").strip(), f"{name}: preference needs a field"
            assert item.get("direction") in PREFERENCE_DIRECTIONS, (
                f"{name}: R19 preferences are scores — direction must be one of "
                f"{sorted(PREFERENCE_DIRECTIONS)}, got {item.get('direction')!r}"
            )
            assert isinstance(item.get("weight"), (int, float)) and not isinstance(
                item.get("weight"), bool
            ), f"{name}: a preference carries a numeric weight, got {item.get('weight')!r}"
            saw_a_preference = True

        for key, golden in expected.items():
            assert _canon(intent.get(key)) == _canon(golden), (
                f"{name}: intent.{key} does not match the golden intent — "
                f"produced {intent.get(key)!r}, expected {golden!r}"
            )

    assert saw_a_constraint, (
        "no golden intent in the fixture set carries a single hard constraint — R19's "
        "filter/score split is not exercised"
    )
    assert saw_a_preference, (
        "no golden intent in the fixture set carries a single preference — R19's "
        "filter/score split is not exercised"
    )


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-071")
def test_no_auction_is_created_before_the_buyer_confirms():
    """R1: no auction is created until the buyer confirms the structured intent."""
    from apps.buyer.svc.src.intent import clarify, confirm

    name, data = _dialogue_fixtures()[0]
    intent = _get(clarify(data["turns"], _ScriptedLLM(data.get("llm_script", []))), "intent")

    withheld = _Recorder(result={"auction_id": "auc-e7-1"})
    try:
        confirm(intent, withheld, confirmed=False)
    except (ImportError, AttributeError, TypeError, NameError):
        raise
    except Exception:
        pass  # refusing an unconfirmed intent by raising is a legitimate shape
    assert withheld.calls == [], (
        f"{name}: confirmation was withheld yet the buyer service called the auction "
        f"client {len(withheld.calls)} time(s): {withheld.calls!r}"
    )

    accepted = _Recorder(result={"auction_id": "auc-e7-1"})
    created = confirm(intent, accepted, confirmed=True)
    assert len(accepted.calls) == 1, (
        f"{name}: a confirmed intent must create exactly one auction, saw "
        f"{len(accepted.calls)} call(s): {accepted.calls!r}"
    )
    assert created is not None, f"{name}: confirm(...) must return the created auction"


# ===================================================================================
# T-070 — R5: coarsened profile, rotating pseudonym, no identity to stores
# ===================================================================================


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-070")
def test_buyer_profile_contains_no_identity_fields():
    """R5: the BuyerProfile handed to stores carries only a pseudonym and coarse buckets."""
    from apps.buyer.svc.src.profile import build_profile

    account = {
        "account_id": "acct-9f3c21",
        "email": "dana.reyes@example.com",
        "first_name": "Dana",
        "last_name": "Reyes",
        "phone": "+1-555-0100",
        "address": "44 Alder Way, Portland OR 97205",
        "postal_code": "97205",
        "region": "US-OR",
        "orders": [
            {"order_ref": "ord-1", "total": 118.0, "category": "running-shoes"},
            {"order_ref": "ord-2", "total": 232.5, "category": "trail-gear"},
            {"order_ref": "ord-3", "total": 96.0, "category": "running-shoes"},
        ],
        "budget_band": "100-250",
    }
    pseudonym = "psn-7c1af204d9"

    profile = build_profile(account, pseudonym)
    plain = _plain(profile)

    assert isinstance(plain, dict), "BuyerProfile must serialize to an object"
    assert set(plain) == {"pseudonym", "buckets"}, (
        f"DESIGN pins BuyerProfile to exactly {{pseudonym, buckets}}, got {sorted(plain)}"
    )
    assert plain["pseudonym"] == pseudonym
    buckets = plain["buckets"]
    assert isinstance(buckets, dict), "BuyerProfile.buckets must be an object"
    assert set(buckets) == PROFILE_BUCKET_KEYS, (
        f"DESIGN pins the buckets to exactly {sorted(PROFILE_BUCKET_KEYS)}, "
        f"got {sorted(buckets)}"
    )
    assert isinstance(buckets["first_time"], bool)
    assert isinstance(buckets["category_affinity"], list)

    for key, _value in _walk(plain):
        assert not IDENTITY_KEY_RE.search(key), (
            f"R5: identity-shaped key {key!r} reached the store-facing BuyerProfile"
        )

    haystack = _blob(plain).lower()
    for secret in (
        "dana",
        "reyes",
        "example.com",
        "555-0100",
        "alder way",
        "acct-9f3c21",
        "97205",
    ):
        assert secret not in haystack, (
            f"R5: buyer identity value {secret!r} leaked into the BuyerProfile: {haystack}"
        )


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-070")
def test_pseudonym_rotates_per_session_and_is_never_reissued():
    """R5: each session gets a fresh pseudonym and a prior pseudonym is never reissued."""
    from apps.buyer.svc.src.vault import PseudonymVault

    vault = PseudonymVault()
    buyer = "dana.reyes@example.com"
    other = "sam.okafor@example.net"

    mine = [vault.issue(buyer) for _ in range(64)]
    theirs = [vault.issue(other) for _ in range(64)]

    for issued in mine + theirs:
        assert isinstance(issued, str) and issued.strip(), (
            f"a pseudonym must be non-empty text, got {issued!r}"
        )

    assert len(set(mine)) == len(mine), (
        f"R5: a prior pseudonym was reissued to the same buyer — {len(mine)} sessions "
        f"produced only {len(set(mine))} distinct pseudonyms"
    )
    assert len(set(theirs)) == len(theirs), (
        "R5: a prior pseudonym was reissued to the second buyer"
    )
    assert set(mine).isdisjoint(set(theirs)), (
        "R5: a pseudonym issued to one buyer was reissued to another"
    )

    for issued in mine:
        lowered = issued.lower()
        for fragment in ("dana", "reyes", "example.com"):
            assert fragment not in lowered, (
                f"R5: the pseudonym {issued!r} embeds buyer identity ({fragment!r})"
            )


# ===================================================================================
# T-072 — R2/R3: provenance labels and the exchange-owned permalink
# ===================================================================================


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-072")
def test_provenance_labels_map_store_confirmed_versus_from_their_website():
    """R2: every Provenance.source maps to its published shortlist label."""
    from apps.buyer.svc.src.accept import provenance_label

    def _provenance(source: str):
        try:
            from packages.contracts import Provenance

            return Provenance(
                source=source,
                ref="src-e7-1",
                observed_at="2026-01-01T00:00:00Z",
                authority_rank=1,
            )
        except Exception:
            return type(
                "_Prov",
                (),
                {
                    "source": source,
                    "ref": "src-e7-1",
                    "observed_at": "2026-01-01T00:00:00Z",
                    "authority_rank": 1,
                },
            )()

    golden = {
        "owner_statement": "store-confirmed",
        "envelope_rule": "store-confirmed",
        "learned_policy": "store-confirmed",
        "pixel_feed": "store-confirmed",
        "network": "store-confirmed",
        "scraped": "from their website",
    }
    for source, expected in golden.items():
        actual = provenance_label(_provenance(source))
        assert actual == expected, (
            f"R2: provenance source {source!r} must be labelled {expected!r}, got {actual!r}"
        )

    asserted = provenance_label(_provenance("seller_asserted"))
    assert asserted not in {"store-confirmed", "from their website"}, (
        f"R2/R8: a seller-asserted claim must not be labelled as store-confirmed or as "
        f"coming from the store's website, got {asserted!r}"
    )
    assert "unverified" in str(asserted).lower(), (
        f"R2: a seller-asserted claim must be labelled unverified, got {asserted!r}"
    )


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-072")
def test_accept_follows_the_permalink_returned_by_the_exchange():
    """R3: the buyer follows the exchange's permalink and never mints a checkout URL."""
    from apps.buyer.svc.src.accept import accept

    permalink = "https://store.example.com/cart/44352913:1?discount=PS-ABC123"
    exchange = _Recorder(result={"permalink_url": permalink})
    slot = {
        "slot": "fit",
        "bid_ref": "bid-e7-7",
        "auction_id": "auc-e7-3",
        "fit_score": 0.91,
        "trust_summary": {"score": 0.72, "confidence": 0.4},
        # A decoy the buyer must ignore: if accept() builds its own URL from the slot,
        # this is what it will build, and the assertions below catch it.
        "checkout_url": "https://attacker.example/cart/1:1?discount=PS-ABC123",
        "variant_id": "44352913",
        "discount_code": "PS-ABC123",
    }

    result = accept(slot, exchange)
    url = result if isinstance(result, str) else _get(result, "permalink_url")

    assert url == permalink, (
        f"R3: the buyer must return the exchange's permalink unmodified — expected "
        f"{permalink!r}, got {url!r}"
    )
    assert len(exchange.calls) == 1, (
        f"R3: accept must delegate to the exchange exactly once, saw {exchange.calls!r}"
    )
    assert "attacker.example" not in _blob(result), (
        "R3: the buyer constructed a checkout URL of its own instead of following the "
        f"exchange's permalink: {_blob(result)}"
    )


# ===================================================================================
# T-073 — R14: one structured feedback prompt, routed orders only
# ===================================================================================


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-073")
def test_feedback_prompt_is_offered_only_for_routed_orders():
    """R14: only network-routed orders get exactly one structured, free-text-free prompt."""
    from apps.buyer.svc.src.feedback import feedback_prompt

    unrouted = feedback_prompt(
        {"order_ref": "ord-e7-100", "store_id": "st-1", "routed": False}
    )
    assert not unrouted, (
        f"R14: an order the network did not route must get no feedback prompt, got "
        f"{unrouted!r}"
    )

    prompt = feedback_prompt(
        {
            "order_ref": "ord-e7-101",
            "store_id": "st-1",
            "auction_id": "auc-e7-3",
            "routed": True,
        }
    )
    assert prompt is not None, "R14: a network-routed order must be offered one prompt"

    plain = _plain(prompt)
    assert isinstance(plain, dict), (
        f"R14: exactly one prompt is offered, not a collection — got {type(plain).__name__}"
    )
    assert str(plain.get("question") or "").strip(), (
        f"R14: the prompt must carry one question, got {plain!r}"
    )

    options = plain.get("options")
    assert isinstance(options, list) and len(options) >= 2, (
        f"R14: the prompt must offer structured options, got {options!r}"
    )

    banned_keys = {
        "free_text",
        "freetext",
        "free_response",
        "open_text",
        "open_response",
        "comment",
        "comments",
        "note",
        "notes",
        "textarea",
        "message",
    }
    banned_types = {"text", "textarea", "string", "free_text", "freetext", "open"}
    for key, value in _walk(plain):
        assert key.lower() not in banned_keys, (
            f"R14: the feedback prompt exposes a free-text field {key!r}: {plain!r}"
        )
        if key.lower() in {"type", "input_type", "input", "kind", "widget"}:
            assert str(value).lower() not in banned_types, (
                f"R14: the feedback prompt offers a free-text input ({key}={value!r})"
            )


@pytest.mark.epic("E7")
@pytest.mark.ticket("T-073")
def test_feedback_submission_lands_as_a_feedback_ledger_event():
    """R14: a submitted response emits exactly one `feedback` LedgerEvent for that order."""
    from apps.buyer.svc.src.feedback import submit_feedback

    sink = _Sink()
    order = {
        "order_ref": "ord-e7-101",
        "store_id": "st-1",
        "auction_id": "auc-e7-3",
        "routed": True,
    }
    response = {"question_id": "matched_pitch", "choice": "yes_as_described"}

    submit_feedback(order, response, sink)

    emitted = [_plain(event) for event in sink.events]
    events = [item for item in emitted if isinstance(item, dict) and "kind" in item]
    assert len(events) == 1, (
        f"R14: one submission must emit exactly one LedgerEvent, sink saw {emitted!r}"
    )

    event = events[0]
    assert str(event.get("kind")) == "feedback", (
        f"R14: the emitted event kind must be 'feedback', got {event.get('kind')!r}"
    )
    assert str(event.get("order_ref")) == "ord-e7-101", (
        f"R14: the feedback event must carry the order ref, got {event.get('order_ref')!r}"
    )

    haystack = _blob(event)
    assert "matched_pitch" in haystack and "yes_as_described" in haystack, (
        f"R14: the structured answer must survive into the ledger event, got {haystack}"
    )
    for key, _value in _walk(event):
        assert not IDENTITY_KEY_RE.search(key), (
            f"R5/R14: identity-shaped key {key!r} reached the feedback ledger event"
        )
