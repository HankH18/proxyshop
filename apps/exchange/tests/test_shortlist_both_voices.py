"""The published slot carries BOTH voices, whose price it is, and a name a person can read (D55).

Three facts a shortlist computed and then threw away at its own boundary, and one property that
must survive their arrival.

1. **WHOSE VOICE.** A store agent writes a per-shopper pitch, puts it on ``Bid.message``, and the
   buyer service carries one back verbatim as ``pitch.store_pitch`` the moment a slot arrives
   holding one. Between them, ``ShortlistSlot`` was ``additionalProperties: false`` and declared
   no message field — so the seller's words died at the contract and every card a shopper saw
   carried the platform's case alone. What a shop buys by joining was the one thing the product
   could not show.
2. **WHOSE PRICE.** When a store does not answer usably the exchange stands in for it at the
   roster's list price (R10), and that number reached the card as ``price`` like any other. The
   auction response said so per store in ``entries[]``; the slot could not pass it on.
3. **WHOSE NAME.** The slot carried ``product_ref`` and ``variant_ref``, so a shopper compared
   two opaque references. The platform's own crawl holds a name for the product and the exchange
   already reads that snapshot to GRADE the store's claims — publishing it is the organic half of
   D55, in the platform's own voice.

And the property, which is the reason this file exists rather than three one-line assertions:
**admitting the seller's prose must not create a channel for unchecked claims.** §4 drives the
liar all the way to the served slot and asserts both halves at once — the message IS published,
verbatim, AND the store still pays the published ``contradicted_claim`` penalty for what it says
in it. Graded, never trusted.
"""

from __future__ import annotations

import time
from typing import Any

from contracts.ranking import CONTRADICTED_CLAIM, DEFAULT_PENALTIES_PER_KIND
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import MAX_SLOT_MESSAGE_CHARS, configure_ranking, slot_message
from exchange.ranking.verification import (
    StaticCatalogSnapshots,
    catalog_identity,
    catalogue_readings,
)
from fastapi.testclient import TestClient

LIVE_FOR_AN_HOUR = 3600.0

HONEST = "store-honest"
LIAR = "store-liar"
SILENT = "store-silent"

#: The two pitches differ in ONE word, and the catalogue below says the warranty is 24 months.
HONEST_PITCH = (
    "This is a heat exchange machine sized for an office queue. "
    "It runs a 9 bar pump and comes with a two-year warranty."
)
LIAR_PITCH = (
    "This is a heat exchange machine sized for an office queue. "
    "It runs a 9 bar pump and comes with a five-year warranty."
)

#: Leading and trailing whitespace, deliberately. It is the shop's bytes; a producer or a
#: consumer that "tidied" them would be editing the seller's sentence somewhere no one can see.
PADDED_PITCH = "  We have been roasting since 1998.\n\n  "


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _snapshot(store_id: str) -> dict[str, Any]:
    """The platform's own catalogue row for one store.

    ``canonical_name`` and ``brand`` are what :func:`catalog_identity` publishes, and
    ``warranty_months`` is what the pitch is graded against.
    """
    return {
        "snapshot_id": f"snap-{store_id}",
        "captured_at": "2026-01-01T00:00:00Z",
        "store_id": store_id,
        "products": [
            {
                "product_ref": "product-1",
                "canonical_name": "Office Espresso Machine",
                "evidence_ref": f"snap-{store_id}#product-1",
                "attributes": {
                    "capacity_l": {"value": 35},
                    "brand": {"value": "Rocketbar"},
                    "boiler_type": {"value": "heat exchange"},
                    "pump_pressure_bar": {"value": 9},
                    "warranty_months": {"value": 24},
                },
            }
        ],
    }


def _catalog(stores: tuple[str, ...]) -> StaticCatalogSnapshots:
    return StaticCatalogSnapshots({store: _snapshot(store) for store in stores})


def _claim(key: str, value: Any) -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "provenance": {"source": "owner_statement", "ref": f"ref:{key}", "authority_rank": 1},
    }


def _offer(store_id: str, price: float = 400.0) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
        "expires_at": time.time() + LIVE_FOR_AN_HOUR,
    }


def _bid(store_id: str, *, message: Any) -> dict[str, Any]:
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": _offer(store_id),
        "claims": [_claim("capacity_l", 35)],
        "message": message,
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }


def _rostered(store_id: str) -> dict[str, Any]:
    return {"store_id": store_id, "tier": 1, "product_ref": "product-1", "list_price": 400.0}


def _intent(*, must_have: bool = True) -> dict[str, Any]:
    """A shopper who asks about the warranty, and optionally states a must-have.

    The ask is what lets ``verified_claim_ratio`` see the pitch at all — the feature is
    buyer-conditional — so both halves of D55's asymmetry are live: the honest store earns on a
    proved relevant fact and the liar pays for a contradicted one.

    ``must_have=False`` is what the R10 tests need, and the reason is R19 rather than
    convenience: a fallback carries an EMPTY claims list by construction, so it satisfies no
    hard constraint and is excluded before it can reach a slot. A shopper who states none is the
    only auction in which a stand-in is visible at all.
    """
    intent: dict[str, Any] = {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "query": "an office espresso machine with a long warranty",
        "hard_constraints": [],
    }
    if must_have:
        intent["hard_constraints"] = [{"field": "capacity_l", "op": "gte", "value": 30}]
    return intent


class Bidders:
    """The outbound bid client, answering from a table. A store not in it never answers."""

    def __init__(self, bids: dict[str, dict[str, Any]]) -> None:
        self.bids = dict(bids)

    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        bid = self.bids.get(store_id)
        if bid is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(bid)}

    __call__ = solicit


def _wired_app(bidders: Bidders, stores: tuple[str, ...]) -> Any:
    app = create_app()
    configure_auctions(
        app,
        solicitor=bidders,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in stores},
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in stores}),
        catalog=_catalog(stores),
    )
    return app


def _post(app: Any, roster: list[dict[str, Any]], *, must_have: bool = True) -> dict[str, Any]:
    response = TestClient(app).post(
        "/auctions",
        json={
            "intent": _intent(must_have=must_have),
            "roster": roster,
            "bid_timeout_seconds": 2.0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _slots_by_store(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {slot["trust_summary"]["store_id"]: slot for slot in body["shortlist"]["slots"]}


# =====================================================================================
# 1. WHOSE VOICE — the shop's own words, on the wire, verbatim
# =====================================================================================
def test_a_served_slot_carries_the_shops_own_message_beside_the_platforms_case() -> None:
    """The defect, closed, over the real HTTP door.

    RED before this change for a reason that had nothing to do with the buyer: the store agent
    wrote the pitch, ``ranking/candidates.py`` projected it, ``ranking/verification.py`` graded
    it — and then ``ShortlistSlot`` had no field to put it in, so the last hop that could have
    carried it dropped it. Every slot the shopper page rendered showed the organic voice alone.
    """
    app = _wired_app(Bidders({HONEST: _bid(HONEST, message=HONEST_PITCH)}), (HONEST,))

    slots = _slots_by_store(_post(app, [_rostered(HONEST)]))

    assert slots[HONEST]["message"] == HONEST_PITCH, slots


def test_the_shops_bytes_are_published_unedited_down_to_the_whitespace() -> None:
    """VERBATIM is the contract, and whitespace is where a tidy-up hides.

    ``buyer_svc.pitch.writing.store_pitch_of`` returns the seller's bytes unchanged and
    ``wire.ts::readPitch`` refuses to trim them, both on the stated ground that this is the last
    hop before a person reads them. A producer that stripped here would make both of those
    promises false while every existing assertion stayed green.
    """
    app = _wired_app(Bidders({HONEST: _bid(HONEST, message=PADDED_PITCH)}), (HONEST,))

    slots = _slots_by_store(_post(app, [_rostered(HONEST)]))

    published = slots[HONEST]["message"]
    assert published == PADDED_PITCH, repr(published)
    assert published.startswith("  "), "leading whitespace was stripped somewhere on the wire"
    assert published.endswith("  "), "trailing whitespace was stripped somewhere on the wire"


def test_a_message_over_the_published_cap_is_refused_whole_and_never_truncated() -> None:
    """The bound, and the one thing it must not do.

    A cut pitch is words the store did not write, attributed to the store. So the cap publishes
    ``null`` instead — and the auction still answers 201 rather than 500ing on the pinned model,
    which is the failure mode a bidder would otherwise control by writing more.
    """
    too_long = "a" * (MAX_SLOT_MESSAGE_CHARS + 1)
    app = _wired_app(Bidders({HONEST: _bid(HONEST, message=too_long)}), (HONEST,))

    slots = _slots_by_store(_post(app, [_rostered(HONEST)]))

    assert slots[HONEST]["message"] is None, "an over-long pitch was published, cut or not"
    at_the_cap = "b" * MAX_SLOT_MESSAGE_CHARS
    assert slot_message({"message": at_the_cap}) == at_the_cap, "the cap is off by one"
    assert slot_message({"message": too_long}) is None


def test_a_message_that_is_not_a_string_publishes_nothing_rather_than_its_repr() -> None:
    """``Bid.message`` is store-written JSON nothing on the auction path schema-validates.

    ``str({"a": 1})`` in front of a shopper, attributed to a shop, is worse than saying nothing.
    """
    for stated in ({"a": 1}, 7, ["a"], "", "   "):
        assert slot_message({"message": stated}) is None, stated

    app = _wired_app(Bidders({HONEST: _bid(HONEST, message={"a": 1})}), (HONEST,))
    slots = _slots_by_store(_post(app, [_rostered(HONEST)]))
    assert slots[HONEST]["message"] is None, slots


# =====================================================================================
# 2. WHOSE PRICE — a real bid and a stand-in, in the same auction
# =====================================================================================
def test_one_auction_shows_a_real_bid_and_a_stand_in_and_the_slots_say_which() -> None:
    """R10's two halves side by side, told apart on the slot for the first time.

    Both slots carry a ``price``, and before ``fallback`` existed the two numbers were
    indistinguishable on the wire: one is what a store offered, the other is what the exchange
    charged on the store's behalf because it never answered. The card either had to present a
    stand-in as a quote or refuse to say anything about either.
    """
    app = _wired_app(Bidders({HONEST: _bid(HONEST, message=HONEST_PITCH)}), (HONEST, SILENT))

    body = _post(app, [_rostered(HONEST), _rostered(SILENT)], must_have=False)
    slots = _slots_by_store(body)
    assert set(slots) == {HONEST, SILENT}, body["excluded"]

    assert slots[HONEST]["fallback"] is False
    assert slots[HONEST]["fallback_reason"] is None, "a real bid has no reason to give"

    assert slots[SILENT]["fallback"] is True
    assert slots[SILENT]["fallback_reason"] == "no_response"
    assert slots[SILENT]["message"] is None, "a store that never spoke has no words to quote"

    # The slot agrees with what the auction reported per store — one fact, two places.
    entries = {entry["store_id"]: entry for entry in body["entries"]}
    for store, slot in slots.items():
        assert slot["fallback"] == entries[store]["fallback"], (store, slot, entries[store])


# =====================================================================================
# 3. WHOSE NAME — the platform's own crawled identity for the product
# =====================================================================================
def test_a_served_slot_names_the_product_in_the_platforms_own_words() -> None:
    """The organic half, published. A shopper reads a name instead of a hash.

    The name is the PLATFORM's — out of the same catalogue snapshot this exchange grades the
    store's claims against — and it carries the snapshot id that produced it, so a reader can
    tell a name decided against the crawl from one decided against a deployment document.
    """
    app = _wired_app(Bidders({HONEST: _bid(HONEST, message=HONEST_PITCH)}), (HONEST,))

    product = _slots_by_store(_post(app, [_rostered(HONEST)]))[HONEST]["product"]

    assert product["product_ref"] == "product-1", product
    assert product["identity"]["title"] == "Office Espresso Machine", product
    assert product["identity"]["brand"] == "Rocketbar", product
    assert product["identity"]["source"] == f"snap-{HONEST}", product
    assert product["identity"]["observed_at"] == "2026-01-01T00:00:00Z", product


def test_a_stand_in_slot_is_named_too_because_it_is_the_one_with_nothing_else() -> None:
    """A fallback keeps nothing the store wrote, so the platform's name is all the card has."""
    app = _wired_app(Bidders({}), (SILENT,))

    product = _slots_by_store(_post(app, [_rostered(SILENT)], must_have=False))[SILENT]["product"]

    assert product["identity"]["title"] == "Office Espresso Machine", product


def test_no_name_is_published_beside_a_reference_it_was_not_resolved_against() -> None:
    """D58's defect class, refused: two objects must never be rendered as one.

    The identity is resolved against the ref the AUCTION named while ``product_ref`` on the slot
    is read off the store's own offer. Where they disagree the shopper is shown a reference with
    no name rather than somebody else's name — there is no join on the card to check it with.
    """
    from exchange.ranking.serving import shortlist_product

    offer = {"product_ref": "product-1"}
    identity = {"title": "Office Espresso Machine", "source": "snap-x"}

    assert shortlist_product(offer, identity, "product-1")["identity"] == identity
    assert shortlist_product(offer, identity, "product-2").get("identity") is None
    assert shortlist_product(offer, identity, None).get("identity") is None


def test_the_catalogue_is_read_once_for_both_answers_it_gives() -> None:
    """The attribute vocabulary and the product name come out of ONE pass.

    ``GraphCatalogSnapshots`` costs an indexed point lookup per call and its header bounds the
    served cost at ``2 x len(roster)`` inside R10's synchronous window. A second reader that
    fetched its own snapshots would have made that ``3 x`` for a field that is a rendering
    rather than a decision.
    """
    asked: list[tuple[str, Any]] = []

    class Counting:
        def snapshot_for(self, store_id: str, product_ref: Any = None) -> Any:
            asked.append((store_id, product_ref))
            return _snapshot(store_id)

    readings = catalogue_readings(
        Counting(), [HONEST, LIAR, HONEST], product_refs={HONEST: "product-1", LIAR: "product-1"}
    )

    assert asked == [(HONEST, "product-1"), (LIAR, "product-1")], asked
    assert readings.identities[HONEST]["title"] == "Office Espresso Machine"
    assert readings.product_refs[HONEST] == "product-1"
    assert {row["key"] for row in readings.attributes or []} >= {"warranty_months"}


def test_an_exchange_that_crawled_nothing_names_nothing_rather_than_guessing() -> None:
    """The fail-closed direction, on the field that is a rendering.

    A missing snapshot, a snapshot with no readable name and a snapshot that will not name
    itself are all ``None``. The last one matters most: an identity with no ``source`` is a name
    a reader cannot trace, which is indistinguishable from one the exchange made up.
    """
    assert catalog_identity(None, "product-1") is None
    assert catalog_identity({"snapshot_id": "s", "products": []}, "product-1") is None
    assert (
        catalog_identity(
            {"snapshot_id": "s", "products": [{"product_ref": "product-1"}]}, "product-1"
        )
        is None
    ), "a row with no canonical_name has no name to publish"
    assert (
        catalog_identity(
            {"products": [{"product_ref": "product-1", "canonical_name": "A"}]}, "product-1"
        )
        is None
    ), "a snapshot that will not name itself must not name a product"


# =====================================================================================
# 4. The property that must survive: the prose is GRADED, never trusted
# =====================================================================================
def test_the_liar_is_still_caught_and_its_words_are_published_anyway() -> None:
    """Both halves at once, and this is the whole point of the file.

    Publishing the seller's message is not a hole in ``additionalProperties: false``. The prose
    reaches ``rank_score`` only through verdicts THIS exchange attested against its own
    catalogue snapshot, under a MAC the bidder cannot compute — so a store cannot score by
    writing prose, it can only be graded on it. What changed is that the shopper now sees the
    sentence the store is being graded on.

    The two bids differ in ONE word. Everything else — price, offer, structured claims, trust
    row, catalogue, domain — is identical, so the score gap is attributable to the prose and to
    nothing else.
    """
    app = _wired_app(
        Bidders(
            {
                HONEST: _bid(HONEST, message=HONEST_PITCH),
                LIAR: _bid(LIAR, message=LIAR_PITCH),
            }
        ),
        (HONEST, LIAR),
    )

    body = _post(app, [_rostered(HONEST), _rostered(LIAR)])

    ranked = {row["store_id"]: row for row in body["ranked"]}
    assert set(ranked) == {HONEST, LIAR}, body["excluded"]

    # CAUGHT: the published penalty, as a scoring COMPONENT of the served response, and its size
    # is the published `contradicted_claim` depth for exactly one contradiction. The honest
    # store, whose every other input is identical, carries no such component at all.
    expected = -DEFAULT_PENALTIES_PER_KIND[CONTRADICTED_CLAIM]
    assert ranked[LIAR]["components"]["policy_penalties"] == expected, ranked[LIAR]
    assert "policy_penalties" not in ranked[HONEST]["components"], ranked[HONEST]
    assert ranked[HONEST]["rank_score"] > ranked[LIAR]["rank_score"], body["ranked"]

    # AND PUBLISHED: the liar's own sentence is on its slot, in full, unedited. Admitting it did
    # not buy it anything — it is the sentence it was penalised for.
    slots = _slots_by_store(body)
    assert slots[LIAR]["message"] == LIAR_PITCH, slots
    assert slots[HONEST]["message"] == HONEST_PITCH, slots
    assert "five-year" in slots[LIAR]["message"], "the claim the exchange contradicted, on screen"


def test_identical_prose_scores_identically_so_the_gap_above_is_the_word() -> None:
    """The arming control. Without it, "the liar ranked below" is worth nothing.

    Any accidental asymmetry between the two stores — solicitation order, domain string, trust
    row — would produce the same assertion above. With the SAME prose the two scores must be
    equal, and both messages must still be published.
    """
    app = _wired_app(
        Bidders(
            {
                HONEST: _bid(HONEST, message=HONEST_PITCH),
                LIAR: _bid(LIAR, message=HONEST_PITCH),
            }
        ),
        (HONEST, LIAR),
    )

    body = _post(app, [_rostered(HONEST), _rostered(LIAR)])

    scores = {row["store_id"]: row["rank_score"] for row in body["ranked"]}
    assert scores[HONEST] == scores[LIAR], body["ranked"]
    slots = _slots_by_store(body)
    assert slots[HONEST]["message"] == slots[LIAR]["message"] == HONEST_PITCH


def test_a_store_cannot_write_its_own_slot_title_its_fallback_flag_or_its_domain() -> None:
    """The three platform facts on the slot, against a bid that states all three itself.

    ``message`` is the only field on this object a bidder authors. A store that writes a title,
    a ``fallback`` of ``false`` or a ``store_domain`` is answered by the PLATFORM's value —
    otherwise a shop would be naming the product a shopper compares, declaring its own
    stand-in price to be a quote, and vouching for its own checkout host.
    """
    forged = _bid(LIAR, message=LIAR_PITCH)
    forged["offer"] = {
        **forged["offer"],
        "canonical_name": "The Best Machine Ever Made",
        "identity": {"title": "The Best Machine Ever Made", "source": "trust-me"},
    }
    forged["store_domain"] = "attacker.example.com"
    forged["fallback"] = False

    app = _wired_app(Bidders({LIAR: forged}), (LIAR,))
    slot = _slots_by_store(_post(app, [_rostered(LIAR)]))[LIAR]

    assert slot["product"]["identity"]["title"] == "Office Espresso Machine", slot
    assert slot["product"]["identity"]["source"] == f"snap-{LIAR}", slot
    assert slot["store_domain"] == _domain(LIAR), slot
    assert slot["fallback"] is False, "the exchange's verdict, which here agrees — it did bid"
    # And the prose it DID author is carried, because that is the field it is allowed to write.
    assert slot["message"] == LIAR_PITCH, slot
