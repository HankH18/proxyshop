"""T-033 — accepting an offer produces a validated code and a permalink (R3, A5, C11, R12).

Ticket verify::

    PROXYSHOP_WORKER=<n> pytest apps/exchange/tests/test_accept.py -q

The first section of this file **is the ticket's gate**, and it is not decoration. A ruling on
the frozen-goal objection about ``test_e3_exchange.py:653`` put a specific requirement on this
ticket: ``checkout.lint.unbound_checkout_requests()`` must run in T-033's own verify. The lint
was *vacuously true* on main — it walks the source tree for ``CheckoutRequest(...)`` built
without a trusted domain source, and there was not one production construction anywhere, so it
could not fail whatever anyone wrote. This branch writes the first one. The three tests below
therefore assert, in order:

1. no ``CheckoutRequest`` in ``apps/exchange/src`` is built unbound (the standing assertion);
2. the accept package really does construct one, so assertion 1 has a subject and is no longer
   vacuous;
3. **the gate can fail** — the real accept module, with the ``registered_domains=`` keyword
   removed by an AST mutation and nothing else changed, is flagged. A gate that cannot go red
   is not a gate, and a run that proves only "the lint returned an empty list" cannot tell the
   difference between "every call site is bound" and "there are no call sites".

Why any of it matters, stated once: the port compares the offer's checkout host against a
"registered seller domain". Unbound, that domain is ``bid["store_domain"]`` — a field the
bidding store wrote — so the guard compares the store's word to the store's word and rejects
only a store that contradicts itself. A bid claiming ``attacker.tld`` with a matching
``checkout_url`` was **measured** being admitted and handed a real single-use discount code.
``test_a_wired_seller_registry_refuses_a_bid_that_lies_consistently`` is that measurement,
inverted into a regression test.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from exchange.accept import (
    ACCEPT_REFUSED,
    AcceptResult,
    accept,
    accept_offer,
    next_slot,
    platform_registered_domains,
    use_registered_domains,
)
from exchange.checkout import (
    CHECKOUT_EVENT_KINDS,
    CODE_PREFIX,
    NoRegisteredDomains,
    StaticRegisteredDomains,
    UnknownCheckoutMode,
    unbound_checkout_requests,
)
from exchange.eligibility import (
    BLACKLISTED,
    ELIGIBLE,
    SELLER_ELIGIBILITY_INTERFACE_VERSION,
    UNAVAILABLE,
    EligibilityDecision,
    SellerEligibility,
    StaticSellerEligibility,
)

EXCHANGE_SRC = Path(__file__).resolve().parents[1] / "src"
ACCEPT_PACKAGE = EXCHANGE_SRC / "accept"

SELLER_DOMAIN = "store-a.example.com"
RIVAL_DOMAIN = "attacker.tld"
T_NOW = 1_700_000_000.0
T_FUTURE = 2_000_000_000.0

#: The platform's registry, as a deployment would wire it: written by us, never by a store.
PLATFORM_DOMAINS = {
    "store-a": "store-a.example.com",
    "store-b": "store-b.example.com",
    "store-c": "store-c.example.com",
    # store-liar is deliberately absent from some tables and present in others.
}


# =====================================================================================
# Doubles
# =====================================================================================
class RecordingCodeCreator:
    """The merchant ``POST /codes`` client, in process. Records every mint it was asked for.

    It builds its permalink on **the offer's own host** on purpose: a creator that
    manufactured an on-domain permalink from the registered domain would make every
    off-domain assertion here unfalsifiable, because the port's second check would then pass
    no matter what host the store sent. Only the query is replaced, so the permalink carries
    the code the merchant claims to have issued.
    """

    def __init__(self, code: str = "PSX-TESTCODE", *, explode_for: tuple[str, ...] = ()) -> None:
        self.code = code
        self.explode_for = set(explode_for)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        self.calls.append((str(store_id), dict(offer)))
        if str(store_id) in self.explode_for:
            raise RuntimeError(f"merchant POST /codes is down for {store_id}")
        base = str(dict(offer).get("checkout_url") or f"https://{SELLER_DOMAIN}/cart/1:1")
        return {"code": self.code, "permalink_url": f"{base.split('?', 1)[0]}?discount={self.code}"}

    __call__ = create_code


class SilentCodeCreator(RecordingCodeCreator):
    """A merchant that answers, but with no code. Never a silent fall back to minting one."""

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        self.calls.append((str(store_id), dict(offer)))
        return {"permalink_url": f"https://{SELLER_DOMAIN}/cart/1:1"}


class RaisingRegisteredDomains:
    """A seller registry that is *down*. Must refuse, never fall back to the bid's claim."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def domain_for(self, store_id: str) -> str | None:
        self.asked.append(store_id)
        raise RuntimeError("seller registry unreachable")


def eligibility_source(
    statuses: dict[str, str],
    *,
    version: str = SELLER_ELIGIBILITY_INTERFACE_VERSION,
    raising: tuple[str, ...] = (),
) -> Any:
    """A `SellerEligibility` over a fixed table, recording who it was asked about."""
    exploding = set(raising)

    class _Source(SellerEligibility):
        interface_version = version

        def __init__(self) -> None:
            self.checked: list[str] = []

        def check(self, store_id: str) -> EligibilityDecision:
            self.checked.append(store_id)
            if store_id in exploding:
                raise RuntimeError(f"eligibility backend unreachable for {store_id}")
            return EligibilityDecision(
                store_id=store_id,
                status=statuses[store_id],
                reason=f"fixture:{statuses[store_id]}",
            )

    return _Source()


def offer(domain: str = SELLER_DOMAIN, *, checkout_url: str | None = None) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": 100.0,
        "total_price": 100.0,
        "checkout_url": (
            checkout_url if checkout_url is not None else f"https://{domain}/cart/1:1?discount=NET"
        ),
        "expires_at": T_FUTURE,
    }


def auction(*bid_ids: str, shortlist: tuple[str, ...] = ()) -> dict[str, Any]:
    """An auction in the frozen suite's shape: `store-x` bids from `store-x.example.com`."""
    bids = []
    for bid_id in bid_ids or ("bid-a", "bid-b"):
        store_id = bid_id.replace("bid", "store")
        domain = f"{store_id}.example.com"
        bids.append(
            {
                "bid_id": bid_id,
                "store_id": store_id,
                "store_domain": domain,
                "offer": offer(domain),
            }
        )
    record: dict[str, Any] = {
        "auction_id": "auction-1",
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "bids": bids,
        "accepted_bid_ref": None,
        "now": T_NOW,
    }
    if shortlist:
        record["shortlist"] = {"slots": [{"bid_ref": ref} for ref in shortlist]}
    return record


def liar_bid() -> dict[str, Any]:
    """A store that writes BOTH halves of the host comparison, and agrees with itself."""
    return {
        "bid_id": "bid-liar",
        "store_id": "store-liar",
        "store_domain": RIVAL_DOMAIN,
        "offer": offer(RIVAL_DOMAIN),
    }


def kinds(result: AcceptResult) -> list[str]:
    return [str(event["kind"]) for event in result.events]


def payload_of(result: AcceptResult, kind: str) -> dict[str, Any]:
    for event in result.events:
        if event["kind"] == kind:
            return dict(event["payload"])
    raise AssertionError(f"no {kind!r} event in {kinds(result)}")


@pytest.fixture
def unwired() -> Iterator[None]:
    """Every test starts with NO platform registry wired, and leaves the process as it found it.

    Module state that leaks between tests would let a test pass because a neighbour wired a
    registry, which is precisely the condition this file exists to detect.
    """
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


# =====================================================================================
# THE GATE — `unbound_checkout_requests`, and the proof it can fail
# =====================================================================================
def test_no_checkout_request_in_the_exchange_is_built_unbound() -> None:
    """Every ``CheckoutRequest`` in exchange source names its trusted domain source."""
    unbound = unbound_checkout_requests(EXCHANGE_SRC)
    assert unbound == [], (
        "the checkout domain guard is decorative at: "
        f"{[str(site) for site in unbound]} — a request built without registered_domains= is "
        "checked against a host the bidding store itself wrote"
    )


def _checkout_request_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "id", None) == "CheckoutRequest"
            or getattr(node.func, "attr", None) == "CheckoutRequest"
        )
    ]


def test_the_accept_package_actually_builds_a_checkout_request() -> None:
    """The lint above has a subject. Without this, an empty result proves nothing.

    ``unbound_checkout_requests`` returned ``[]`` on main for the only reason a check can be
    worthless: there was nothing in the tree for it to look at. This ticket writes the first
    production call site, and that is what turns the standing assertion from vacuous into
    binding — so the presence of the call site is itself asserted.
    """
    built = [
        path
        for path in sorted(ACCEPT_PACKAGE.rglob("*.py"))
        if _checkout_request_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    ]
    assert built, (
        f"no CheckoutRequest is constructed anywhere in {ACCEPT_PACKAGE} — accept() is not "
        "going through the T-036 port, and the unbound-request lint has nothing to check"
    )


def _mutated_source(*, drop_trusted_source: bool) -> tuple[str, int]:
    """Re-emit the real accept module, optionally without ``registered_domains=``.

    Mutating the AST rather than the text is deliberate: a regex over source lines would keep
    passing after a reformat and would quietly stop mutating anything, which turns a positive
    control into a second copy of the negative one.
    """
    path = ACCEPT_PACKAGE / "offer.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = _checkout_request_calls(tree)
    if drop_trusted_source:
        for call in calls:
            call.keywords = [kw for kw in call.keywords if kw.arg != "registered_domains"]
    return ast.unparse(tree), len(calls)


def test_the_unbound_lint_flags_this_ticket_s_own_call_site_when_the_keyword_is_removed(
    tmp_path: Path,
) -> None:
    """THE POSITIVE CONTROL: the gate goes red on the real code with one keyword removed.

    Both halves run against the same AST round-trip, so the only difference between the
    negative and the positive control is the ``registered_domains=`` keyword itself — not
    formatting, not the unparse, not the temporary directory.
    """
    clean, call_count = _mutated_source(drop_trusted_source=False)
    assert call_count >= 1, "nothing to mutate: accept builds no CheckoutRequest"

    (tmp_path / "clean").mkdir()
    (tmp_path / "clean" / "offer.py").write_text(clean, encoding="utf-8")
    assert unbound_checkout_requests(tmp_path / "clean") == [], (
        "the accept module flags itself even unmutated — the lint is flagging the round-trip, "
        "so the positive control below would prove nothing"
    )

    mutated, _ = _mutated_source(drop_trusted_source=True)
    assert mutated != clean, "the mutation changed nothing; registered_domains= was not found"
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "offer.py").write_text(mutated, encoding="utf-8")

    flagged = unbound_checkout_requests(tmp_path / "broken")
    assert [site.path.name for site in flagged] == ["offer.py"], (
        "removing registered_domains= from accept()'s CheckoutRequest did NOT fail the lint. "
        "This ticket's gate cannot go red, so a green run is not evidence of anything."
    )


# =====================================================================================
# R3 / C11 — the permalink and the golden event sequence
# =====================================================================================
@pytest.mark.parametrize("mode", ["redirect", "shopify_stub", "shopify"])
def test_accept_returns_a_permalink_on_the_sellers_domain_and_the_golden_events(
    mode: str, unwired: None
) -> None:
    creator = RecordingCodeCreator()
    result = accept(auction(), "bid-a", creator, mode)

    assert result.accepted is True
    assert result.denial_reason is None
    assert result.store_id == "store-a"
    assert isinstance(result.permalink_url, str) and result.permalink_url
    assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN
    assert result.code and result.code in result.permalink_url
    assert result.checkout_token
    assert kinds(result) == list(CHECKOUT_EVENT_KINDS)
    assert payload_of(result, "checkout_redirect")["permalink_url"] == result.permalink_url


def test_the_simulated_redirect_provider_needs_no_merchant_client(unwired: None) -> None:
    """D45: the starting slice runs with no Shopify, no merchant app and no injected client."""
    result = accept(auction(), "bid-a", None, "redirect")
    assert result.accepted is True
    assert result.code is not None and result.code.startswith(CODE_PREFIX)


def test_shopify_mode_delegates_code_creation_exactly_once(unwired: None) -> None:
    creator = RecordingCodeCreator()
    result = accept(auction(), "bid-a", creator, "shopify")
    assert [store for store, _ in creator.calls] == ["store-a"]
    assert result.code == creator.code


def test_every_mode_emits_the_identical_ordered_event_kinds(unwired: None) -> None:
    """C11: nothing downstream may be able to tell which provider ran."""
    emitted = {
        mode: kinds(accept(auction(), "bid-a", RecordingCodeCreator(), mode))
        for mode in ("redirect", "shopify_stub", "shopify")
    }
    assert len(set(map(tuple, emitted.values()))) == 1, f"event kinds diverge by mode: {emitted}"


def test_an_unregistered_checkout_mode_raises_rather_than_running_the_simulated_path(
    unwired: None,
) -> None:
    creator = RecordingCodeCreator()
    with pytest.raises(UnknownCheckoutMode):
        accept(auction(), "bid-a", creator, "paypal-someday")
    assert creator.calls == [], "a code was created for a mode with no registered provider"


# =====================================================================================
# R3 / A5 — one accept per auction
# =====================================================================================
def test_a_second_accept_issues_no_second_code_and_no_second_permalink(unwired: None) -> None:
    creator = RecordingCodeCreator()
    live = auction()

    first = accept(live, "bid-a", creator, "shopify")
    assert first.accepted is True
    assert live["accepted_bid_ref"] == "bid-a", "the auction was not stamped as accepted"

    second = accept(live, "bid-b", creator, "shopify")
    assert second.accepted is False
    assert second.permalink_url is None
    assert second.code is None
    assert "code_created" not in kinds(second)
    assert len(creator.calls) == 1, f"a second discount code was created: {creator.calls}"
    assert "already" in (second.denial_reason or "").lower()


def test_a_refused_accept_leaves_the_auction_acceptable(unwired: None) -> None:
    """A refusal must not close the auction — otherwise one bad bid burns the whole purchase."""
    live = auction("bid-a", "bid-b")
    live["bids"][0]["offer"]["checkout_url"] = f"https://{RIVAL_DOMAIN}/cart/1:1"

    refused = accept(live, "bid-a", RecordingCodeCreator(), "redirect")
    assert refused.accepted is False
    assert live["accepted_bid_ref"] is None, "a refused accept stamped the auction"

    recovered = accept(live, "bid-b", RecordingCodeCreator(), "redirect")
    assert recovered.accepted is True
    assert urlsplit(str(recovered.permalink_url)).hostname == "store-b.example.com"


def test_a_refusal_is_recorded_as_a_policy_event_never_as_an_accept(unwired: None) -> None:
    """D24's kinds are frozen; a refusal is filed as what it is rather than inventing a kind."""
    live = auction()
    accept(live, "bid-a", RecordingCodeCreator(), "shopify")
    second = accept(live, "bid-b", RecordingCodeCreator(), "shopify")

    assert kinds(second) == ["policy_event"]
    payload = payload_of(second, "policy_event")
    assert payload["kind"] == ACCEPT_REFUSED
    assert payload["bid_ref"] == "bid-b"
    assert payload["reason"] == second.denial_reason


# =====================================================================================
# A5 — a failure re-offers the next slot
# =====================================================================================
def test_a_merchant_code_creation_failure_re_offers_the_next_slot(unwired: None) -> None:
    live = auction("bid-a", "bid-b")
    creator = RecordingCodeCreator(explode_for=("store-a",))

    failed = accept(live, "bid-a", creator, "shopify")
    assert failed.accepted is False
    assert failed.permalink_url is None
    assert failed.reoffer_bid_ref == "bid-b", "the buyer was left with no next slot"
    assert "store-a" in [store for store, _ in creator.calls]

    recovered = accept(live, failed.reoffer_bid_ref, creator, "shopify")
    assert recovered.accepted is True
    assert urlsplit(str(recovered.permalink_url)).hostname == "store-b.example.com"


def test_a_merchant_that_answers_without_a_code_is_a_failure_not_a_local_mint(
    unwired: None,
) -> None:
    """A code the merchant never issued is a discount nobody agreed to honour."""
    creator = SilentCodeCreator()
    result = accept(auction(), "bid-a", creator, "shopify")
    assert result.accepted is False
    assert result.code is None
    assert len(creator.calls) == 1


def test_the_re_offer_follows_shortlist_order_when_the_auction_carries_one(
    unwired: None,
) -> None:
    """A5 says "the next slot", and the slot order is the order the buyer was shown."""
    live = auction("bid-a", "bid-b", "bid-c", shortlist=("bid-c", "bid-a", "bid-b"))
    failed = accept(live, "bid-c", RecordingCodeCreator(explode_for=("store-c",)), "shopify")
    assert failed.reoffer_bid_ref == "bid-a", "the re-offer ignored the published slot order"

    assert next_slot(live, "bid-b") is None, "there is no slot after the last one"
    assert next_slot(live, "bid-c", exhausted=("bid-a",)) == "bid-b"


def test_accepting_a_bid_the_auction_does_not_carry_is_refused(unwired: None) -> None:
    creator = RecordingCodeCreator()
    result = accept(auction(), "bid-nowhere", creator, "shopify")
    assert result.accepted is False
    assert creator.calls == []
    assert "unknown_bid" in (result.denial_reason or "")


# =====================================================================================
# C10 / D22 — the domain guard, and the source that makes it real
# =====================================================================================
@pytest.mark.parametrize(
    "label,hostile",
    [
        ("rival domain", f"https://{RIVAL_DOMAIN}/cart/1:1?discount=X"),
        ("suffix spoof", f"https://evil-{SELLER_DOMAIN}.attacker.tld/cart/1:1?discount=X"),
        ("glued suffix spoof", f"https://evil-{SELLER_DOMAIN}/cart/1:1?discount=X"),
        ("userinfo spoof", f"https://{SELLER_DOMAIN}@attacker.tld/cart/1:1?discount=X"),
        ("subdomain", f"https://checkout.{SELLER_DOMAIN}/cart/1:1?discount=X"),
        ("javascript scheme", "javascript:alert(1)"),
    ],
)
def test_an_off_domain_checkout_url_is_refused_before_any_code_is_created(
    label: str, hostile: str, unwired: None
) -> None:
    live = auction()
    live["bids"][0]["offer"]["checkout_url"] = hostile
    creator = RecordingCodeCreator()

    result = accept(live, "bid-a", creator, "shopify")

    assert result.accepted is False, f"{label}: an off-domain checkout URL was accepted"
    assert result.permalink_url is None
    assert creator.calls == [], f"{label}: a discount code was created before the host check"


def test_without_a_wired_registry_the_guard_is_decorative_and_says_so(unwired: None) -> None:
    """The MEASURED defect, pinned as behaviour so nobody mistakes the green suite for safety.

    A store that writes ``attacker.tld`` into ``store_domain`` AND into ``checkout_url`` has
    supplied both halves of the comparison, so the exact-host check passes. It cannot be
    refused at runtime — with no external source, an honest bid and this one are byte
    identical — so the port records that nothing was verified, and this test is what keeps
    that record honest. The fix is the next test: wire the registry.
    """
    assert platform_registered_domains() is None
    live = auction()
    live["bids"].append(liar_bid())

    result = accept(live, "bid-liar", RecordingCodeCreator(), "redirect")

    assert result.accepted is True, "unwired, the port has no information to refuse on"
    assert urlsplit(str(result.permalink_url)).hostname == RIVAL_DOMAIN
    assert result.domain_verified is False, (
        "an unverified checkout claimed to be platform-verified — a decorative pass and a "
        "real one must not look identical from the outside"
    )
    assert payload_of(result, "checkout_redirect")["domain_verified"] is False


def test_a_wired_seller_registry_refuses_a_bid_that_lies_consistently(unwired: None) -> None:
    """The fix. The platform's table overrides the bid outright, and the liar mints nothing."""
    sellers = StaticRegisteredDomains({**PLATFORM_DOMAINS, "store-liar": "store-liar.example.com"})
    live = auction()
    live["bids"].append(liar_bid())
    creator = RecordingCodeCreator()

    refused = accept(live, "bid-liar", creator, "redirect", registered_domains=sellers)
    assert refused.accepted is False
    assert refused.permalink_url is None
    assert creator.calls == []
    assert RIVAL_DOMAIN in (refused.denial_reason or "")

    # Positive control: a registry that refuses everybody would satisfy the assertions above.
    honest = accept(live, "bid-a", creator, "redirect", registered_domains=sellers)
    assert honest.accepted is True, "the wired registry refused an honest store too"
    assert honest.domain_verified is True
    assert urlsplit(str(honest.permalink_url)).hostname == SELLER_DOMAIN
    assert payload_of(honest, "checkout_redirect")["domain_verified"] is True


def test_a_seller_the_platform_has_never_registered_mints_nothing(unwired: None) -> None:
    live = auction()
    live["bids"].append(liar_bid())
    creator = RecordingCodeCreator()

    result = accept(
        live, "bid-liar", creator, "shopify", registered_domains=StaticRegisteredDomains({})
    )
    assert result.accepted is False
    assert creator.calls == []

    fail_closed = accept(
        live, "bid-a", creator, "shopify", registered_domains=NoRegisteredDomains()
    )
    assert fail_closed.accepted is False, "the fail-closed default admitted a checkout"
    assert creator.calls == []


def test_a_registry_that_raises_refuses_rather_than_trusting_the_bid(unwired: None) -> None:
    """Falling back on a lookup failure would let a store honour its own claim by breaking it."""
    registry = RaisingRegisteredDomains()
    result = accept(
        auction(), "bid-a", RecordingCodeCreator(), "redirect", registered_domains=registry
    )
    assert result.accepted is False
    assert registry.asked == ["store-a"]


def test_the_wired_default_is_used_when_the_caller_passes_nothing(unwired: None) -> None:
    """`use_registered_domains` is the deployment seam: wire once, every accept is bound."""
    use_registered_domains(StaticRegisteredDomains(PLATFORM_DOMAINS))
    live = auction()
    live["bids"].append(liar_bid())

    assert accept(live, "bid-liar", RecordingCodeCreator(), "redirect").accepted is False
    bound = accept(live, "bid-a", RecordingCodeCreator(), "redirect")
    assert bound.accepted is True
    assert bound.domain_verified is True


def test_an_explicit_none_overrides_the_wired_default(unwired: None) -> None:
    """ "Not passed" and "passed None" are different requests and must not collapse."""
    use_registered_domains(StaticRegisteredDomains(PLATFORM_DOMAINS))
    result = accept(auction(), "bid-a", RecordingCodeCreator(), "redirect", registered_domains=None)
    assert result.accepted is True
    assert result.domain_verified is False


# =====================================================================================
# R12 — eligibility is re-read at accept time
# =====================================================================================
def test_accept_offer_is_exported_from_the_orchestration_boundary() -> None:
    """D54 puts both gates on one public boundary; the frozen suite reads it from there."""
    from exchange.orchestration import accept_offer as boundary  # noqa: PLC0415

    assert boundary is accept_offer


def test_an_eligible_store_is_accepted_and_the_interface_was_actually_consulted(
    unwired: None,
) -> None:
    source = eligibility_source({"store-a": ELIGIBLE, "store-b": ELIGIBLE})
    creator = RecordingCodeCreator()

    result = accept_offer(
        auction=auction(), bid_ref="bid-a", code_creator=creator, mode="shopify", eligibility=source
    )
    assert result.accepted is True
    assert result.denial_reason is None
    assert source.checked == ["store-a"], "the accept gate did not consult eligibility"
    assert len(creator.calls) == 1


@pytest.mark.parametrize("status,token", [(BLACKLISTED, "blacklist"), (UNAVAILABLE, "unavailable")])
def test_a_store_that_became_ineligible_after_bidding_gets_no_code(
    status: str, token: str, unwired: None
) -> None:
    creator = RecordingCodeCreator()
    result = accept_offer(
        auction=auction(),
        bid_ref="bid-a",
        code_creator=creator,
        mode="shopify",
        eligibility=eligibility_source({"store-a": status, "store-b": ELIGIBLE}),
    )
    assert result.accepted is False
    assert result.permalink_url is None
    assert creator.calls == []
    assert token in (result.denial_reason or "").lower()
    assert result.reoffer_bid_ref == "bid-b", "one store's ban must not end the auction"


def test_an_eligibility_read_that_raises_refuses_the_accept(unwired: None) -> None:
    creator = RecordingCodeCreator()
    result = accept_offer(
        auction=auction(),
        bid_ref="bid-a",
        code_creator=creator,
        mode="shopify",
        eligibility=eligibility_source(
            {"store-a": ELIGIBLE, "store-b": ELIGIBLE}, raising=("store-a",)
        ),
    )
    assert result.accepted is False
    assert creator.calls == []
    assert "unavailable" in (result.denial_reason or "").lower()


def test_the_gate_is_per_store_and_not_a_blanket_refusal(unwired: None) -> None:
    """A surface that refuses every accept satisfies every negative assertion above."""
    creator = RecordingCodeCreator()
    result = accept_offer(
        auction=auction(),
        bid_ref="bid-b",
        code_creator=creator,
        mode="shopify",
        eligibility=eligibility_source({"store-a": BLACKLISTED, "store-b": ELIGIBLE}),
    )
    assert result.accepted is True, "banning store-a refused store-b's accept"
    assert len(creator.calls) == 1


def test_an_eligibility_source_speaking_another_interface_version_is_not_trusted(
    unwired: None,
) -> None:
    creator = RecordingCodeCreator()
    result = accept_offer(
        auction=auction(),
        bid_ref="bid-a",
        code_creator=creator,
        mode="shopify",
        eligibility=eligibility_source(
            {"store-a": ELIGIBLE, "store-b": ELIGIBLE}, version="0.0.0-not-published"
        ),
    )
    assert result.accepted is False
    assert result.permalink_url is None
    assert creator.calls == []


def test_no_eligibility_source_at_all_refuses(unwired: None) -> None:
    """ "Nobody wired a gate" is not evidence that the store passed one."""
    creator = RecordingCodeCreator()
    result = accept_offer(auction=auction(), bid_ref="bid-a", code_creator=creator, mode="shopify")
    assert result.accepted is False
    assert creator.calls == []


def test_the_accept_gate_carries_the_registered_domain_source_through(unwired: None) -> None:
    """The gate must not lose the trusted domain on its way to the port."""
    live = auction()
    live["bids"].append(liar_bid())
    creator = RecordingCodeCreator()
    eligible = StaticSellerEligibility(
        {"store-a": ELIGIBLE, "store-liar": ELIGIBLE}, default=ELIGIBLE
    )
    sellers = StaticRegisteredDomains({**PLATFORM_DOMAINS, "store-liar": "store-liar.example.com"})

    refused = accept_offer(
        auction=live,
        bid_ref="bid-liar",
        code_creator=creator,
        mode="redirect",
        eligibility=eligible,
        registered_domains=sellers,
    )
    assert refused.accepted is False
    assert creator.calls == []

    allowed = accept_offer(
        auction=live,
        bid_ref="bid-a",
        code_creator=creator,
        mode="redirect",
        eligibility=eligible,
        registered_domains=sellers,
    )
    assert allowed.accepted is True
    assert allowed.domain_verified is True
