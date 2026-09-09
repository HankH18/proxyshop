"""The tier-0 fallback handoff, and the one invariant it turns on.

Run it on its own::

    PROXYSHOP_WORKER=11 .venv/bin/python -m pytest \\
        apps/exchange/tests/test_fallback_handoff.py -q

The ruling this file grades, verbatim
--------------------------------------

    "It seems to me like the fallback should be basically the same thing that we offer at
    tier 0. If a store isn't responding, then we just treat them as a random merchant, don't
    give a discount, and direct customers to their checkout instead of having anything
    native."

So an accept on a list-price fallback (R10) **succeeds**: it mints no discount code, it hands
back the store's own checkout destination with nothing on it, and it says in words that no
discount applies. What it replaced was an interim 409 ``fallback_not_purchasable``, shipped
while the question was open and documented in three places as pending a ruling.

THE INVARIANT
-------------

    **No discount code is minted against a fallback under any configuration.**

"Under any configuration" is worth nothing if the configurations that matter are not among
the ones driven, so this file drives them by name rather than asserting the property once on
a convenient fixture:

* **``NullSolicitor`` — the DEFAULT.** An exchange with no outbound bid client asks nobody,
  so EVERY rostered store falls back and the whole shortlist is the caller's own roster at
  prices the caller wrote, on an unauthenticated request. That is the configuration the hole
  was measured in, and a green run that skipped it would prove almost nothing.
* **A roster price the caller invented.** ``5e-324`` and ``1e308`` — the two the hole was
  measured at. The T-177 price wall cannot catch either, because
  ``collect_bids._price_refusal`` runs only when a reply ARRIVED: ``0.01`` is refused when a
  store bids it and admitted when a caller writes it on the roster. So the wall is not what
  keeps these from becoming discounts; the absence of a mint is.
* **Every registered checkout mode, with and without a claim table, with and without a
  merchant client.** Read out of the registry rather than listed here, so a provider added
  later is swept too.

Each of those has a **control** beside it, because an exchange that minted nothing for
anybody would satisfy every assertion above. The controls are real accepts that DO mint.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from exchange.accept import (
    DENIAL_ALREADY_ACCEPTED,
    DENIAL_UNROUTABLE_FALLBACK,
    accept,
    denial_code,
    use_registered_domains,
)
from exchange.checkout import StaticRegisteredDomains, registered_modes
from exchange.composition import ENV_DEPLOYMENT, ENV_DEPLOYMENT_JSON
from exchange.main import create_app
from fastapi.testclient import TestClient

# =====================================================================================
# A composed exchange running on the DEFAULT solicitor
# =====================================================================================
#: The stores this deployment registers. No ``bid_endpoint`` on any of them, which is the
#: whole point: the composition root has no outbound client to build, so the wired solicitor
#: is ``auction.routes.NullSolicitor`` — the default — and every store falls back.
STORES: tuple[dict[str, Any], ...] = (
    {"store_id": "s1", "domain": "s1.example.com"},
    {"store_id": "s2", "domain": "s2.example.com"},
)

INTENT: dict[str, Any] = {
    "intent_id": "intent-fallback-handoff-1",
    "cluster_id": "cluster-1",
    "query": "a purchase from a store that never answered",
    "hard_constraints": [],
    "preferences": [],
    "created_at": "2026-01-01T00:00:00Z",
    "schema_version": "1.0.0",
}


def _deployment_document() -> dict[str, Any]:
    """The document a person writes to deploy an exchange with no store agents wired."""
    return {
        "sellers": [
            {
                "store_id": row["store_id"],
                "eligibility": "eligible",
                "registered_domain": row["domain"],
            }
            for row in STORES
        ],
        "trust_snapshot": {
            "stores": {
                row["store_id"]: {"store_id": row["store_id"], "blacklisted": False, "score": 0.8}
                for row in STORES
            }
        },
        "checkout_mode": "redirect",
    }


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain seam exactly as this file found it.

    ``configure_accept(registered_domains=…)`` — which the composition root reaches through —
    also turns a module-level seam, so a file that wires an app and does not restore it
    changes what every later test in the process reads. Same fixture, same reason, as
    ``test_accept_routes.py``'s and ``test_composition_root.py``'s.
    """
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


@pytest.fixture
def deployed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None
) -> Iterator[TestClient]:
    """A client onto ``create_app()``, configured the way a deployment configures it.

    Nothing here calls a ``configure_*`` seam: the app reads the deployment document for
    itself, which is what makes "the default solicitor" a property of the DEPLOYMENT rather
    than of a fixture that wired one.
    """
    document = tmp_path / "deployment.json"
    document.write_text(json.dumps(_deployment_document(), indent=2), encoding="utf-8")
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)
    with TestClient(create_app()) as client:
        yield client


def _open(client: TestClient, list_price: float) -> dict[str, Any]:
    response = client.post(
        "/auctions",
        json={
            "intent": INTENT,
            "profile": {"pseudonym": "psn-fallback-1", "buckets": {}},
            "roster": [
                {
                    "store_id": row["store_id"],
                    "tier": 1,
                    "product_ref": "prod-1",
                    "list_price": list_price,
                    "max_discount_pct": 20.0,
                }
                for row in STORES
            ],
        },
    )
    assert response.status_code == 201, f"POST /auctions -> {response.status_code}: {response.text}"
    return response.json()


def _assert_every_entry_fell_back(body: dict[str, Any]) -> None:
    """The armer: this run really is the configuration the invariant is about.

    Without it, a deployment change that quietly gave the stores a bid client would leave
    every "no code was minted" assertion below true for the uninteresting reason that no
    fallback was ever accepted.
    """
    entries = body.get("entries") or []
    assert entries, f"the auction collected no entries at all: {body}"
    not_fallbacks = [e["store_id"] for e in entries if not e.get("fallback")]
    assert not not_fallbacks, (
        f"{not_fallbacks} answered their solicitation, so this exchange is NOT running on the "
        "default NullSolicitor and the invariant below is being graded on the wrong "
        "configuration"
    )


# =====================================================================================
# THE INVARIANT, under the default solicitor
# =====================================================================================
def test_under_the_default_solicitor_a_shortlisted_fallback_is_handed_over_with_no_code(
    deployed: TestClient,
) -> None:
    """R10 still holds, and accepting what it shows mints nothing.

    Both halves in one request on purpose. "Reached the shortlist" and "was not minted
    against" are separate properties, and dropping the fallback out of the shortlist would
    satisfy the second while destroying the first.
    """
    body = _open(deployed, 100.0)
    _assert_every_entry_fell_back(body)

    slots = body["shortlist"]["slots"]
    assert slots, f"R10: a silent store must still reach the shortlist — {body['excluded']}"
    ref = str(slots[0]["bid_ref"])

    accepted = deployed.post(f"/auctions/{body['auction_id']}/accept", json={"bid_ref": ref})
    payload = accepted.json()
    assert accepted.status_code == 200, accepted.text
    assert payload["code"] is None, f"a fallback minted a discount code: {payload}"
    assert "discount" not in payload["permalink_url"], payload
    # `/cart/1:1` was a placeholder pinned as a contract, and the URL it named is a 404.
    # `services/shopify-stub/src/app.py` passes `1` through its `isdigit()` gate and then
    # answers `404 {"errors": "Variant 1 is not available"}`, because `state.variants.get(1)`
    # is None; and over `fixtures/real-catalogs-demo` the nineteen stores' own variant ids run
    # 9 to 14 digits (histogram `{9: 12, 10: 3, 11: 23, 12: 1, 13: 73, 14: 28022}`), so no
    # store in the corpus issues variant 1. `contracts.protocol.Offer.variant_ref` already
    # said so in words: "absent means 'the bid did not name one', never 'the default
    # variant'". A fallback whose row names no variant is now sent to the seller's own front
    # door — on the registered domain, so S8 is unchanged — instead of to a cart for something
    # nobody sells. This file's real claims (accepted, no code minted, no discount on the URL)
    # are untouched.
    assert payload["permalink_url"] == f"https://{STORES[0]['domain']}/", payload
    assert "/cart/1:" not in payload["permalink_url"], payload
    assert payload["notice"] and "No discount applies" in payload["notice"], payload


@pytest.mark.parametrize("list_price", [5e-324, 1e308], ids=["denormal-min", "near-float-max"])
def test_a_roster_price_the_caller_invented_still_mints_no_code(
    deployed: TestClient, list_price: float
) -> None:
    """The two prices the hole was measured at, on an unauthenticated request.

    ``POST /auctions`` takes no credential and the roster is the caller's own document, so
    ``list_price`` here is a number nobody sold anything at. The point is not that these are
    refused — they are not, and the T-177 price wall is deliberately not what stops them; see
    this module's docstring. The point is that whatever number the caller writes, the answer
    carries no discount code, so there is nothing for a merchant to be asked to honour.
    """
    body = _open(deployed, list_price)
    _assert_every_entry_fell_back(body)

    slots = body["shortlist"]["slots"]
    assert slots, f"the absurd price emptied the shortlist, so nothing was accepted: {body}"
    ref = str(slots[0]["bid_ref"])

    accepted = deployed.post(f"/auctions/{body['auction_id']}/accept", json={"bid_ref": ref})
    payload = accepted.json()
    assert accepted.status_code == 200, accepted.text
    assert payload["code"] is None, (
        f"a caller-written list_price of {list_price!r} became a live discount code: {payload}"
    )
    assert payload["notice"], payload


def test_the_second_accept_is_refused_so_a_handoff_is_not_a_free_extra_offer(
    deployed: TestClient,
) -> None:
    """Taking the handoff SPENDS the auction, exactly as a minting accept does.

    Without this, a buyer could accept the fallback, be sent to the store's own checkout, and
    then accept again for a real code — one auction, two acceptances. The claim
    (:mod:`exchange.accept.claims`) is taken on this path for that reason, and this is what
    says so from outside.
    """
    body = _open(deployed, 100.0)
    _assert_every_entry_fell_back(body)
    refs = [str(slot["bid_ref"]) for slot in body["shortlist"]["slots"]]
    assert len(refs) >= 2, f"this test needs a second slot to try; got {refs}"

    first = deployed.post(f"/auctions/{body['auction_id']}/accept", json={"bid_ref": refs[0]})
    assert first.status_code == 200, first.text
    assert first.json()["code"] is None, first.text

    second = deployed.post(f"/auctions/{body['auction_id']}/accept", json={"bid_ref": refs[1]})
    assert second.status_code == 409, second.text
    assert second.json()["accepted"] is False, second.text


# =====================================================================================
# THE INVARIANT, swept over every configuration `accept()` itself has
# =====================================================================================
SELLER_DOMAIN = "store-a.example.com"


class RecordingCreator:
    """A merchant client that mints a real-looking code — and remembers being asked.

    The remembering is the load-bearing half. ``code is None`` says the exchange returned no
    discount; ``calls == []`` says no merchant was ever ASKED for one, which is the property
    that survives a future refactor deciding to mint and then discard.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        self.calls.append(str(store_id))
        base = str(dict(offer).get("checkout_url") or f"https://{SELLER_DOMAIN}/cart/1:1")
        return {"code": "PSX-TESTCODE", "permalink_url": f"{base}?discount=PSX-TESTCODE"}

    __call__ = create_code


class InMemoryClaims:
    """The T-158 claim port, at-most-once, in one process. Enough to be a real claim table."""

    def __init__(self) -> None:
        self._held: dict[str, str] = {}

    def claim(self, auction_id: str, bid_ref: str) -> Any:
        holder = self._held.setdefault(str(auction_id), str(bid_ref))
        return type("Outcome", (), {"won": holder == str(bid_ref), "holder": holder})()

    def release(self, auction_id: str, bid_ref: str) -> None:
        if self._held.get(str(auction_id)) == str(bid_ref):
            self._held.pop(str(auction_id), None)


def _auction(*, fallback: bool, store_domain: str = SELLER_DOMAIN) -> dict[str, Any]:
    """One auction whose ``bid-a`` is fully mintable except for the ``fallback`` flag.

    The offer is left complete on purpose: an offer the checkout port would have refused
    anyway would make "no code was minted" true for a reason that has nothing to do with the
    flag, and the control below would then be measuring nothing.
    """
    bid: dict[str, Any] = {
        "bid_id": "bid-a",
        "store_id": "store-a",
        "store_domain": store_domain,
        "offer": {
            "product_ref": "product-1",
            "unit_price": 100.0,
            "total_price": 100.0,
            "checkout_url": f"https://{SELLER_DOMAIN}/cart/1:1",
            "expires_at": 2_000_000_000.0,
        },
    }
    if fallback:
        bid["fallback"] = True
    return {
        "auction_id": "auction-1",
        "bids": [bid, {**bid, "bid_id": "bid-b", "store_id": "store-b"}],
        "accepted_bid_ref": None,
        "now": 1_700_000_000.0,
    }


def _registry() -> Any:
    return StaticRegisteredDomains({"store-a": SELLER_DOMAIN, "store-b": "store-b.example.com"})


def _configurations() -> list[tuple[str, dict[str, Any]]]:
    """Every ``accept()`` configuration this file sweeps, labelled.

    The modes come from :func:`~exchange.checkout.registered_modes`, never from a literal, so
    a provider registered later is swept without anyone remembering to add it here.
    """
    cases: list[tuple[str, dict[str, Any]]] = []
    for mode in sorted(registered_modes()):
        for claim_label, claims in (("claims", InMemoryClaims()), ("no-claims", None)):
            for creator_label, creator in (("creator", RecordingCreator()), ("no-creator", None)):
                cases.append(
                    (
                        f"{mode}/{claim_label}/{creator_label}",
                        {"mode": mode, "claims": claims, "code_creator": creator},
                    )
                )
    return cases


def test_the_configuration_sweep_is_armed() -> None:
    """A sweep that swept nothing would make the invariant test below vacuously green."""
    cases = _configurations()
    modes = sorted(registered_modes())
    assert modes, "the checkout registry declares no modes; the sweep below drives nothing"
    assert len(cases) == len(modes) * 4, f"the sweep is {len(cases)} cases over {modes}"
    assert len(cases) >= 8, f"only {len(cases)} configuration(s) are swept"
    assert len({label for label, _ in cases}) == len(cases), "two configurations share a label"


def test_no_configuration_mints_a_discount_code_against_a_fallback(unwired: None) -> None:
    """THE INVARIANT, stated once and driven over every configuration there is."""
    minted: list[str] = []
    asked: list[str] = []
    silent: list[str] = []
    for label, kwargs in _configurations():
        creator = kwargs["code_creator"]
        result = accept(
            _auction(fallback=True),
            "bid-a",
            creator,
            kwargs["mode"],
            registered_domains=_registry(),
            claims=kwargs["claims"],
        )
        if not result.accepted:
            silent.append(f"{label} -> {result.denial_reason}")
            continue
        if result.code is not None:
            minted.append(f"{label} -> {result.code!r}")
        if creator is not None and creator.calls:
            asked.append(f"{label} -> create_code{tuple(creator.calls)}")
        # `/cart/1:1` was a placeholder pinned as a contract, and the URL it named is a 404.
        # `services/shopify-stub/src/app.py` passes `1` through its `isdigit()` gate and then
        # answers `404 {"errors": "Variant 1 is not available"}`, because `state.variants.get(1)`
        # is None; and over `fixtures/real-catalogs-demo` the nineteen stores' own variant ids run
        # 9 to 14 digits (histogram `{9: 12, 10: 3, 11: 23, 12: 1, 13: 73, 14: 28022}`), so no
        # store in the corpus issues variant 1. `contracts.protocol.Offer.variant_ref` already
        # said so in words: "absent means 'the bid did not name one', never 'the default
        # variant'". A fallback whose row names no variant is now sent to the seller's own front
        # door — on the registered domain, so S8 is unchanged — instead of to a cart for something
        # nobody sells. This file's real claims (accepted, no code minted, no discount on the URL)
        # are untouched.
        assert result.permalink_url == f"https://{SELLER_DOMAIN}/", label
        assert result.discount_notice, f"{label}: the buyer was told nothing"
        assert "code_created" not in result.kinds, f"{label}: {result.kinds}"

    assert not minted, "a fallback was minted a discount code in:\n  " + "\n  ".join(minted)
    assert not asked, "a merchant was asked to mint for a fallback in:\n  " + "\n  ".join(asked)
    assert not silent, (
        "these configurations refused rather than handing the buyer over, so the invariant "
        "was not actually exercised in them:\n  " + "\n  ".join(silent)
    )


def test_the_same_sweep_mints_for_a_real_bid(unwired: None) -> None:
    """The control. An accept path that minted for NOBODY would pass the test above.

    Same auction, same configurations, one difference: the ``fallback`` flag is not stamped.
    Every configuration that can mint at all must mint here, and the assertion names the ones
    that did not so a silently dead sweep cannot read as a clean run.
    """
    minted: list[str] = []
    unminted: list[str] = []
    for label, kwargs in _configurations():
        result = accept(
            _auction(fallback=False),
            "bid-a",
            kwargs["code_creator"],
            kwargs["mode"],
            registered_domains=_registry(),
            claims=kwargs["claims"],
        )
        target = minted if (result.accepted and result.code) else unminted
        target.append(f"{label} -> accepted={result.accepted} code={result.code!r}")

    assert minted, (
        "not one configuration minted a code for a store that actually bid, so the invariant "
        "test above is passing because nothing mints at all:\n  " + "\n  ".join(unminted)
    )
    # `redirect` mints locally and `shopify` needs the merchant client, so a `no-creator`
    # shopify cell legitimately does not mint. What must not happen is EVERY cell failing.
    assert len(minted) >= len(unminted), (
        f"more configurations failed to mint than minted: minted {minted}, did not {unminted}"
    )


# =====================================================================================
# What the handoff refuses to borrow
# =====================================================================================
def test_a_fallback_is_never_sent_to_the_domain_the_bid_claimed(unwired: None) -> None:
    """The destination comes from the PLATFORM's registry or from nowhere (C10/D22/S8).

    With no registry wired, ``registered_domain_for`` normally falls back to the bid's own
    ``store_domain`` — and on a fallback the store never answered, so a ``store_domain``
    sitting on that bid is not something the store said either. It is refused rather than
    followed, which is what keeps a manufactured entry from becoming a redirect to a host the
    platform never registered.
    """
    result = accept(
        _auction(fallback=True, store_domain="attacker.tld"),
        "bid-a",
        RecordingCreator(),
        "shopify",
        registered_domains=None,
    )
    assert result.accepted is False, f"a fallback was routed with no registry: {result}"
    assert denial_code(result.denial_reason) == DENIAL_UNROUTABLE_FALLBACK, result.denial_reason
    assert result.permalink_url is None, result
    assert "attacker.tld" not in str(result.permalink_url), result


def test_the_same_bid_without_the_flag_does_follow_the_registry(unwired: None) -> None:
    """The control for the refusal above: the flag is what makes the registry mandatory."""
    result = accept(
        _auction(fallback=False),
        "bid-a",
        RecordingCreator(),
        "shopify",
        registered_domains=_registry(),
    )
    assert result.accepted is True, result.denial_reason
    assert result.code, result
    assert result.permalink_url.startswith(f"https://{SELLER_DOMAIN}/"), result


def test_accepting_a_fallback_closes_the_auction_to_a_second_accept(unwired: None) -> None:
    """One acceptance per auction, handoff included — the object-scoped half of the guard."""
    board = _auction(fallback=True)
    first = accept(board, "bid-a", RecordingCreator(), "shopify", registered_domains=_registry())
    assert first.accepted is True, first.denial_reason
    assert first.code is None, first

    creator = RecordingCreator()
    second = accept(board, "bid-b", creator, "shopify", registered_domains=_registry())
    assert second.accepted is False, "the handoff left the auction open to a second accept"
    assert denial_code(second.denial_reason) == DENIAL_ALREADY_ACCEPTED, second.denial_reason
    assert creator.calls == [], f"a second accept reached the merchant: {creator.calls}"


@pytest.mark.parametrize(
    "first_is_fallback", [True, False], ids=["handoff-then-real", "real-then-handoff"]
)
def test_a_handoff_and_a_real_accept_cannot_both_happen_on_one_auction(
    unwired: None, first_is_fallback: bool
) -> None:
    """The crossing case, which the same-kind test above cannot reach.

    Every other second-accept test in this repo takes the same KIND of bid twice. The
    dangerous pair is the mixed one: take the free handoff, then come back for a real
    discount code on the same auction — one auction, two acceptances, one of them minted.
    Both orders are driven, because the guard could plausibly hold in one direction and not
    the other: the handoff and the minting path stamp the auction from different places.
    """
    board = _auction(fallback=True)
    board["bids"][1]["fallback"] = not first_is_fallback
    board["bids"][0]["fallback"] = first_is_fallback

    first_creator, second_creator = RecordingCreator(), RecordingCreator()
    first = accept(board, "bid-a", first_creator, "shopify", registered_domains=_registry())
    assert first.accepted is True, first.denial_reason

    second = accept(board, "bid-b", second_creator, "shopify", registered_domains=_registry())
    assert second.accepted is False, (
        f"one auction produced two acceptances: {first.bid_ref} then {second.bid_ref}"
    )
    assert denial_code(second.denial_reason) == DENIAL_ALREADY_ACCEPTED, second.denial_reason

    codes = [c for c in (first.code, second.code) if c]
    assert len(codes) <= 1, f"one auction minted more than one code: {codes}"
    assert second.code is None, second
    assert second_creator.calls == [], f"the second accept reached the merchant: {second_creator}"


def test_a_bid_whose_offer_is_not_a_mapping_refuses_instead_of_burning_the_auction(
    unwired: None,
) -> None:
    """A malformed bid record must cost this accept, not the whole auction.

    ``accept()`` reads the offer as ``_read(bid, "offer") or {}``, which is not a mapping
    check, so a truthy non-mapping reaches the handoff intact. Building the ledger events
    from it used to raise AFTER the auction had been stamped and the claim taken: HTTP 500,
    no code, no destination, no re-offer, and every later accept on that auction answering
    ``already_accepted``. The minting path treats the same input as an ordinary refusal, so
    the handoff was the weaker of the two doors.
    """
    board = _auction(fallback=True)
    board["bids"][0]["offer"] = [("product_ref", "p1")]

    # Before the fix this line RAISED — `ValueError: dictionary update sequence element #0
    # has length 1; 2 is required`, straight out of `accept()`, i.e. an HTTP 500 — with the
    # auction already stamped and its claim already taken.
    result = accept(board, "bid-a", RecordingCreator(), "shopify", registered_domains=_registry())

    assert result.code is None, result

    # The property is NOT "it must refuse". An empty offer body is a fine thing to record for
    # an entry the exchange manufactured. The property is that the auction is spent if and
    # only if the buyer got an answer — so there is never a stamped auction with nothing to
    # show for it, which is exactly what the crash left behind.
    stamped = bool(board.get("accepted_bid_ref"))
    assert stamped == bool(result.accepted), (
        f"stamped={stamped} but accepted={result.accepted}: the auction was spent without "
        f"producing an answer, or produced one without being spent"
    )
    if result.accepted:
        # `/cart/1:1` was a placeholder pinned as a contract, and the URL it named is a 404.
        # `services/shopify-stub/src/app.py` passes `1` through its `isdigit()` gate and then
        # answers `404 {"errors": "Variant 1 is not available"}`, because `state.variants.get(1)`
        # is None; and over `fixtures/real-catalogs-demo` the nineteen stores' own variant ids run
        # 9 to 14 digits (histogram `{9: 12, 10: 3, 11: 23, 12: 1, 13: 73, 14: 28022}`), so no
        # store in the corpus issues variant 1. `contracts.protocol.Offer.variant_ref` already
        # said so in words: "absent means 'the bid did not name one', never 'the default
        # variant'". A fallback whose row names no variant is now sent to the seller's own front
        # door — on the registered domain, so S8 is unchanged — instead of to a cart for something
        # nobody sells. This file's real claims (accepted, no code minted, no discount on the URL)
        # are untouched.
        assert result.permalink_url == f"https://{SELLER_DOMAIN}/", result
        assert result.discount_notice, result
    else:
        assert result.reoffer_bid_ref, "the buyer was left with no next slot"
        # ...and the auction is still usable, which is what "not burned" means.
        follow_up = accept(
            board, "bid-b", RecordingCreator(), "shopify", registered_domains=_registry()
        )
        assert follow_up.accepted, (
            f"the malformed bid burned the auction: {follow_up.denial_reason}"
        )


def test_the_handoff_records_the_two_events_that_happened_and_not_the_third(
    unwired: None,
) -> None:
    """C11 for a handoff, and the absent ``code_created`` is the record, not an omission."""
    result = accept(
        _auction(fallback=True),
        "bid-a",
        RecordingCreator(),
        "shopify",
        registered_domains=_registry(),
    )
    assert result.accepted is True, result.denial_reason
    assert result.kinds == ["accepted", "checkout_redirect"], result.kinds
    redirect = [e for e in result.events if e["kind"] == "checkout_redirect"][0]
    # `/cart/1:1` was a placeholder pinned as a contract, and the URL it named is a 404.
    # `services/shopify-stub/src/app.py` passes `1` through its `isdigit()` gate and then
    # answers `404 {"errors": "Variant 1 is not available"}`, because `state.variants.get(1)`
    # is None; and over `fixtures/real-catalogs-demo` the nineteen stores' own variant ids run
    # 9 to 14 digits (histogram `{9: 12, 10: 3, 11: 23, 12: 1, 13: 73, 14: 28022}`), so no
    # store in the corpus issues variant 1. `contracts.protocol.Offer.variant_ref` already
    # said so in words: "absent means 'the bid did not name one', never 'the default
    # variant'". A fallback whose row names no variant is now sent to the seller's own front
    # door — on the registered domain, so S8 is unchanged — instead of to a cart for something
    # nobody sells. This file's real claims (accepted, no code minted, no discount on the URL)
    # are untouched.
    assert redirect["payload"]["permalink_url"] == f"https://{SELLER_DOMAIN}/"
    assert redirect["payload"]["discount_applied"] is False, redirect
    assert redirect["payload"]["fallback"] is True, redirect
    # The module docstring promises this field is on the redirect event; the handoff's used
    # to omit it, which made that sentence true of only one of the two paths it describes.
    assert redirect["payload"]["domain_verified"] is True, redirect
    # ...and the promise the reconciler would grade a webhook against is still written down.
    opened = [e for e in result.events if e["kind"] == "accepted"][0]
    assert opened["payload"]["offer"]["total_price"] == 100.0, opened


def test_a_successful_handoff_reports_that_the_platform_vouched_for_the_host(
    unwired: None,
) -> None:
    """``domain_verified`` must be earned, not hardcoded optimism.

    It is the flag that stops a decorative host check from looking like a real one, so it
    needs a test that would notice it becoming a lie. The pair below is what makes it a
    measurement rather than a constant: it is ``True`` exactly when a platform registry
    answered, and the case where none does produces no result to read it off at all.
    """
    verified = accept(
        _auction(fallback=True),
        "bid-a",
        RecordingCreator(),
        "shopify",
        registered_domains=_registry(),
    )
    assert verified.accepted is True, verified.denial_reason
    assert verified.domain_verified is True, (
        "the platform registry answered and the handoff did not record that it had"
    )

    unverifiable = accept(
        _auction(fallback=True), "bid-a", RecordingCreator(), "shopify", registered_domains=None
    )
    assert unverifiable.accepted is False, (
        "with no platform registry there is nothing to vouch for the host, so there must be "
        "no successful handoff carrying domain_verified at all"
    )


@pytest.mark.parametrize(
    "row",
    [
        "good.tld:8080@evil.tld",
        "https://x.com",
        "  spaced.tld  ",
        "evil.tld/../good.tld",
    ],
)
def test_a_malformed_registry_row_cannot_become_a_destination(unwired: None, row: str) -> None:
    """S8 on the handoff: no checkout URL is returned off the seller's registered domain.

    ``fallback_checkout_url`` interpolates the registry value VERBATIM — deliberately, so
    that this layer never quietly repairs the platform's own record — and its docstring says
    the malformed result is caught downstream by ``is_on_domain``. That was true of its
    original consumer in the ranking filters and NOT true of this one, so the handoff
    returned 200 with URLs like ``https://good.tld:8080@evil.tld/cart/1:1``, whose effective
    host is ``evil.tld``. The minting path refused the identical rows.

    A store cannot write a registry row, so this is the platform's own data integrity rather
    than a spoof anyone can mount — which is a reason to fail closed cheaply, not a reason to
    skip the check.
    """
    result = accept(
        _auction(fallback=True),
        "bid-a",
        RecordingCreator(),
        "shopify",
        registered_domains=StaticRegisteredDomains({"store-a": row}),
    )
    assert result.accepted is False, (
        f"registry row {row!r} produced a handoff to {result.permalink_url!r}"
    )
    assert denial_code(result.denial_reason) == DENIAL_UNROUTABLE_FALLBACK, result.denial_reason
    assert result.permalink_url is None, result


def test_a_well_formed_registry_row_still_produces_a_destination(unwired: None) -> None:
    """The control for the four rows above: the check refuses malformed rows, not all rows."""
    result = accept(
        _auction(fallback=True),
        "bid-a",
        RecordingCreator(),
        "shopify",
        registered_domains=_registry(),
    )
    assert result.accepted is True, result.denial_reason
    # `/cart/1:1` was a placeholder pinned as a contract, and the URL it named is a 404.
    # `services/shopify-stub/src/app.py` passes `1` through its `isdigit()` gate and then
    # answers `404 {"errors": "Variant 1 is not available"}`, because `state.variants.get(1)`
    # is None; and over `fixtures/real-catalogs-demo` the nineteen stores' own variant ids run
    # 9 to 14 digits (histogram `{9: 12, 10: 3, 11: 23, 12: 1, 13: 73, 14: 28022}`), so no
    # store in the corpus issues variant 1. `contracts.protocol.Offer.variant_ref` already
    # said so in words: "absent means 'the bid did not name one', never 'the default
    # variant'". A fallback whose row names no variant is now sent to the seller's own front
    # door — on the registered domain, so S8 is unchanged — instead of to a cart for something
    # nobody sells. This file's real claims (accepted, no code minted, no discount on the URL)
    # are untouched.
    assert result.permalink_url == f"https://{SELLER_DOMAIN}/", result
