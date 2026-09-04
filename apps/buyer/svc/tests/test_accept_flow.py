"""R3: the buyer follows the exchange's permalink and mints nothing (T-072).

The frozen acceptance suite proves the happy path: one call, the permalink back unmodified,
and no URL built out of the slot's decoy ``checkout_url``. This file is the adversarial
half. `accept()` ends in a REDIRECT, so it is attacked as one — every spoof shape that
defeats a weaker host comparison, the schemes that have no host at all, and the two
measured upstream defects (an empty ``auction_id`` off ``intent.confirm``, and an exchange
that answers without a permalink).
"""

from __future__ import annotations

import pytest

from apps.buyer.svc.src.accept import (
    AcceptedOffer,
    AcceptError,
    AcceptRefusedByExchange,
    ExchangeClientUnusable,
    MissingAuctionReference,
    NoPermalinkReturned,
    OffDomainPermalink,
    OfferAlreadyAccepted,
    UnsafePermalink,
    UnusableSlot,
    accept,
    reset_accepted,
)

PERMALINK = "https://store-x.example.com/cart/44352913:1?discount=PS-ABC123"
STORE = "store-x.example.com"


#: `None` is a real answer to test (a client that returned nothing at all), so the double's
#: "use the default" marker cannot be `None`.
_DEFAULT = object()


class Exchange:
    """A recording exchange client. Every call lands in ``.calls``."""

    def __init__(self, result=_DEFAULT, boom: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.result = {"permalink_url": PERMALINK} if result is _DEFAULT else result
        self.boom = boom

    def accept_offer(self, payload):
        self.calls.append(payload)
        if self.boom is not None:
            raise self.boom
        return self.result


def slot(**overrides):
    base = {
        "slot": "fit",
        "bid_ref": "bid-e7-7",
        "auction_id": "auc-e7-3",
        "fit_score": 0.91,
        "trust_summary": {"score": 0.72, "confidence": 0.4},
        # The decoy. R3 says the buyer never follows it and never builds from it.
        "checkout_url": "https://attacker.example/cart/1:1?discount=PS-ABC123",
        "variant_id": "44352913",
        "discount_code": "PS-ABC123",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _fresh_ledger():
    reset_accepted()
    yield
    reset_accepted()


# --- the handoff itself -------------------------------------------------------------


def test_the_exchange_is_asked_exactly_once_and_told_which_bid() -> None:
    exchange = Exchange()
    result = accept(slot(), exchange)

    assert isinstance(result, AcceptedOffer)
    assert result.permalink_url == PERMALINK
    assert exchange.calls == [{"auction_id": "auc-e7-3", "bid_ref": "bid-e7-7"}]


def test_the_receipt_never_echoes_the_slots_own_checkout_url() -> None:
    """The published projection is narrow on purpose — the decoy cannot ride out on it."""
    result = accept(slot(), Exchange())
    published = result.to_dict()

    assert "attacker.example" not in repr(published)
    assert set(published) == {
        "permalink_url",
        "auction_id",
        "bid_ref",
        "slot",
        "accepted_at",
        "called",
    }
    assert result["permalink_url"] == PERMALINK  # subscriptable, like ClarifyOutcome


def test_a_bare_callable_client_works_and_is_still_called_once() -> None:
    seen: list[dict] = []

    def client(payload):
        seen.append(payload)
        return {"permalink_url": PERMALINK}

    assert accept(slot(), client).called == "__call__"
    assert len(seen) == 1


def test_the_permalink_comes_back_byte_for_byte() -> None:
    """Not normalised, not re-encoded, not re-hosted. R3 is about authority, not tidiness."""
    odd = "https://store-x.example.com/cart/44352913:1?discount=PS-ABC123&utm=a%20b#frag"
    assert accept(slot(), Exchange({"permalink_url": odd})).permalink_url == odd


# --- the two measured upstream defects ----------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_an_empty_auction_id_is_refused_before_the_exchange_is_touched(blank) -> None:
    """`intent.confirm` really does return `auction_id == ""` — see its WARNING branch."""
    exchange = Exchange()
    with pytest.raises(MissingAuctionReference):
        accept(slot(auction_id=blank), exchange)
    assert exchange.calls == []


def test_a_missing_bid_ref_is_refused_before_the_exchange_is_touched() -> None:
    exchange = Exchange()
    with pytest.raises(MissingAuctionReference):
        accept(slot(bid_ref=""), exchange)
    assert exchange.calls == []


@pytest.mark.parametrize("junk", ["", "auc-e7-3", None, 7, 0.5, True])
def test_something_that_is_not_a_slot_is_a_domain_refusal_not_a_type_error(junk) -> None:
    exchange = Exchange()
    with pytest.raises(UnusableSlot) as caught:
        accept(junk, exchange)
    assert isinstance(caught.value, AcceptError)
    assert not isinstance(caught.value, (TypeError, ValueError, AttributeError))
    assert exchange.calls == []


# --- what the exchange answered ------------------------------------------------------


@pytest.mark.parametrize("answer", [{}, {"code": "PS-ABC123"}, {"permalink_url": ""}, None])
def test_an_answer_with_no_permalink_refuses_rather_than_minting_one(answer) -> None:
    with pytest.raises(NoPermalinkReturned):
        accept(slot(), Exchange(answer))


def test_a_denied_accept_carries_the_exchanges_own_reason() -> None:
    denied = {"accepted": False, "denial_reason": "blacklist"}
    with pytest.raises(AcceptRefusedByExchange) as caught:
        accept(slot(), Exchange(denied))
    assert caught.value.denial_reason == "blacklist"


def test_no_client_at_all_is_a_named_refusal() -> None:
    with pytest.raises(ExchangeClientUnusable):
        accept(slot(), None)


def test_a_client_with_no_accept_method_is_a_named_refusal() -> None:
    with pytest.raises(ExchangeClientUnusable):
        accept(slot(), object())


# --- the redirect, attacked as a redirect --------------------------------------------

SPOOFS = [
    pytest.param("https://evil.example/cart/1:1", id="rival-domain"),
    pytest.param("https://store-x.example.com.evil.example.com/c", id="suffix-spoof"),
    pytest.param("https://evil-store-x.example.com/c", id="glued-prefix"),
    pytest.param("https://store-x.example.com:8443@evil.example.com/c", id="userinfo"),
    pytest.param("https://checkout.store-x.example.com/c", id="subdomain"),
    pytest.param("https://STORE-X.EXAMPLE.COM.evil.tld/c", id="uppercase-suffix-spoof"),
]


@pytest.mark.parametrize("spoofed", SPOOFS)
def test_a_permalink_off_the_stores_domain_is_refused(spoofed) -> None:
    with pytest.raises(OffDomainPermalink):
        accept(slot(store_domain=STORE), Exchange({"permalink_url": spoofed}))


def test_the_stores_own_domain_is_accepted_including_a_trailing_dot() -> None:
    """The control case: without it every assertion above could pass by refusing everything."""
    assert accept(slot(store_domain=STORE), Exchange()).permalink_url == PERMALINK
    reset_accepted()
    assert accept(slot(store_domain="STORE-X.Example.Com."), Exchange()).permalink_url == PERMALINK


UNSAFE = [
    pytest.param("javascript:alert(document.cookie)", id="javascript"),
    pytest.param("data:text/html,<script>1</script>", id="data"),
    pytest.param("//evil.example/cart/1:1", id="protocol-relative"),
    pytest.param("ftp://store-x.example.com/c", id="ftp"),
    pytest.param("https:///cart/1:1", id="empty-authority"),
    pytest.param("https://:8443/cart/1:1", id="no-host"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("  https://store-x.example.com/c  ", id="padded"),
    pytest.param("https://store-x.example.com\n@evil.example/c", id="newline-smuggle"),
    pytest.param("https://store-x.example.com\t@evil.example/c", id="tab-smuggle"),
]


@pytest.mark.parametrize("hostile", UNSAFE)
def test_a_permalink_a_browser_must_not_follow_is_refused(hostile) -> None:
    """No expected domain here — these are refused on their own shape, not on their host."""
    with pytest.raises((UnsafePermalink, NoPermalinkReturned)):
        accept(slot(), Exchange({"permalink_url": hostile}))


def test_the_newline_smuggle_would_have_reached_evil_example() -> None:
    """Why the control-character rule exists, spelled out rather than asserted abstractly."""
    from urllib.parse import urlsplit

    smuggled = "https://store-x.example.com\n@evil.example/c"
    assert urlsplit(smuggled).hostname == "evil.example"
    assert "store-x.example.com" in smuggled


def test_the_empty_authority_shape_is_read_differently_by_the_two_parsers() -> None:
    """Why `EMPTY_AUTHORITY` exists: the server and the browser disagree about this URL.

    ``urlsplit`` reads ``https:///cart/1:1`` as having no host. The browser's WHATWG parser
    skips the extra slash and resolves it to the host ``cart`` — measured with node on this
    worktree, and asserted from the TypeScript side in
    ``apps/buyer/app/shortlist/shortlist.test.tsx``. Both halves refuse it, and both refuse
    it for the same stated reason rather than agreeing by accident.
    """
    from urllib.parse import urlsplit

    from apps.buyer.svc.src.accept import reason_unsafe

    assert urlsplit("https:///cart/1:1").hostname is None
    assert "authority" in str(reason_unsafe("https:///cart/1:1"))


def test_an_off_domain_refusal_is_still_an_accept_error() -> None:
    with pytest.raises(AcceptError):
        accept(slot(store_domain=STORE), Exchange({"permalink_url": "https://evil.example/c"}))


def test_the_slots_own_checkout_url_is_never_used_as_the_expected_domain() -> None:
    """A spoofed slot must not be able to certify a permalink pointing at itself."""
    hostile = slot(checkout_url="https://attacker.example/cart/1:1")
    with pytest.raises(OffDomainPermalink):
        accept(
            hostile,
            Exchange({"permalink_url": "https://attacker.example/cart/1:1"}),
            expected_domain=STORE,
        )


# --- one auction, one checkout --------------------------------------------------------


def test_a_second_accept_of_the_same_auction_is_refused_and_carries_the_first_permalink() -> None:
    first = accept(slot(), Exchange())
    with pytest.raises(OfferAlreadyAccepted) as caught:
        accept(slot(bid_ref="bid-e7-8"), Exchange())
    assert caught.value.permalink_url == first.permalink_url


def test_a_failed_accept_can_honestly_be_retried() -> None:
    """The claim is released when the call itself fails, or an outage would be permanent."""
    with pytest.raises(RuntimeError):
        accept(slot(), Exchange(boom=RuntimeError("connection reset")))
    assert accept(slot(), Exchange()).permalink_url == PERMALINK


def test_a_refused_permalink_also_releases_the_claim() -> None:
    with pytest.raises(UnsafePermalink):
        accept(slot(), Exchange({"permalink_url": "javascript:alert(1)"}))
    assert accept(slot(), Exchange()).permalink_url == PERMALINK
