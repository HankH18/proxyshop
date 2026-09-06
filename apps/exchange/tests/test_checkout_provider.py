"""T-036 — the `CheckoutProvider` port, its registry, and the guarantees it holds for all.

Ticket verify: ``pytest apps/exchange/tests/test_checkout_provider.py -q``.

The port's whole reason to exist is that three properties hold for **every** provider,
including ones nobody has written yet:

1. the offer's checkout host is checked against the registered seller domain *before*
   anything is minted (D22 / C10 / S8-3);
2. every provider emits the identical ordered `LedgerEvent` kinds, so nothing downstream can
   tell which one ran (C11);
3. nothing outside this package mints a discount code at all.

Each is tested by **denial with a positive control**: a surface that refuses everything
satisfies a refusal assertion and is not a port.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from exchange.checkout import (
    CHECKOUT_EVENT_KINDS,
    CHECKOUT_MODES,
    CODE_PREFIX,
    DEFAULT_CHECKOUT_MODE,
    CheckoutCreatorError,
    CheckoutProvider,
    CheckoutRequest,
    MintedCheckout,
    OffDomainCheckout,
    OrphanedCheckoutCode,
    OrphanedCode,
    OrphanedOffDomainCheckout,
    PortMethodIsFinal,
    ShopifyCheckoutProvider,
    SimulatedRedirectProvider,
    UnknownCheckoutMode,
    UnusableDiscount,
    UnusableOffer,
    code_expiry,
    code_fingerprint,
    code_minting_call_sites,
    is_on_domain,
    mint_code,
    minting_ledger,
    offer_discount_percentage,
    register_provider,
    registered_modes,
    resolve_provider,
    shopify_discount_percentage,
)

SELLER_DOMAIN = "store-a.example.com"
T_NOW = 1_700_000_000.0
T_FUTURE = 2_000_000_000.0

CHECKOUT_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "checkout"
EXCHANGE_SRC = Path(__file__).resolve().parents[1] / "src"


class RecordingCodeCreator:
    """The merchant ``POST /codes`` client, in process. Records every mint it was asked for."""

    def __init__(self, code: str = "PROXY-TEST-CODE", domain: str = SELLER_DOMAIN) -> None:
        self.code = code
        self.permalink = f"https://{domain}/cart/1:1?discount={code}"
        self.calls: list[tuple[str, Any]] = []

    def create_code(self, store_id: str, offer: Any) -> dict[str, str]:
        self.calls.append((store_id, offer))
        return {"code": self.code, "permalink_url": self.permalink}

    __call__ = create_code


def offer(checkout_url: str | None = None) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": 100.0,
        "total_price": 100.0,
        "checkout_url": checkout_url or f"https://{SELLER_DOMAIN}/cart/1:1?discount=NET",
        "expires_at": T_FUTURE,
    }


def request(
    *,
    mode: str = "redirect",
    checkout_url: str | None = None,
    creator: Any | None = None,
    domain: str = SELLER_DOMAIN,
) -> CheckoutRequest:
    return CheckoutRequest(
        auction_id="auction-1",
        bid_ref="bid-a",
        store_id="store-a",
        store_domain=domain,
        offer=offer(checkout_url),
        mode=mode,
        code_creator=creator,
        now=T_NOW,
    )


# --- the five hostile hosts, each defeating a different sloppy comparison --------------
SPOOFS = {
    "plain other domain": "https://attacker.tld/cart/1:1?discount=X",
    "suffix spoof": f"https://evil-{SELLER_DOMAIN}.attacker.tld/cart/1:1?discount=X",
    "glued suffix spoof": f"https://evil-{SELLER_DOMAIN}/cart/1:1?discount=X",
    "userinfo spoof": f"https://{SELLER_DOMAIN}@attacker.tld/cart/1:1?discount=X",
    "subdomain": f"https://checkout.{SELLER_DOMAIN}/cart/1:1?discount=X",
}


# =====================================================================================
# Acceptance 1 — one port, a provider per CHECKOUT_MODE spelling, no silent fallback
# =====================================================================================
def test_every_checkout_mode_spelling_resolves_to_a_provider() -> None:
    assert set(CHECKOUT_MODES) <= set(registered_modes())
    for mode in CHECKOUT_MODES:
        assert isinstance(resolve_provider(mode), CheckoutProvider)
    assert DEFAULT_CHECKOUT_MODE == "redirect"
    assert isinstance(resolve_provider(None), SimulatedRedirectProvider)


@pytest.mark.parametrize("mode", ["paypal", "stripe_checkout", "", "REDIRECTT"])
def test_an_unregistered_mode_raises_rather_than_falling_back(mode: str) -> None:
    """An `if/else` chain's `else` silently becomes the behaviour of every unplanned mode."""
    with pytest.raises(UnknownCheckoutMode) as raised:
        resolve_provider(mode)
    assert "registered modes are" in str(raised.value)


def test_mode_spellings_are_matched_case_and_whitespace_insensitively() -> None:
    assert resolve_provider("  Shopify_Stub ") is resolve_provider("shopify_stub")


def test_a_provider_that_is_not_a_checkout_provider_cannot_be_registered() -> None:
    """Registering a bare callable would put a minting path outside the port's guarantees."""

    class NotAProvider:
        def checkout(self, req: CheckoutRequest) -> None:
            return None

    with pytest.raises(TypeError):
        register_provider("rogue", NotAProvider())  # type: ignore[arg-type]
    assert "rogue" not in registered_modes()


def test_no_code_is_minted_anywhere_outside_the_checkout_package() -> None:
    """The architectural claim T-036 exists to make, checked mechanically over the tree."""
    sites = code_minting_call_sites(EXCHANGE_SRC)
    assert sites, "the scanner found no minting call site at all — it is not looking at code"
    outside = [site for site in sites if CHECKOUT_PACKAGE not in site.path.parents]
    assert outside == [], f"discount codes are minted outside the port: {[str(s) for s in outside]}"


def test_the_minting_lint_catches_a_call_site_planted_outside_the_port(tmp_path: Path) -> None:
    """The positive control: a lint that cannot fail is not a lint."""
    (tmp_path / "accept").mkdir()
    (tmp_path / "accept" / "shortcut.py").write_text(
        "def accept(auction, bid_id, code_creator, mode):\n"
        "    return code_creator.create_code(auction['store_id'], {})\n",
        encoding="utf-8",
    )
    # Forwarding the creator without invoking it is legitimate and must NOT be flagged.
    (tmp_path / "accept" / "forwards.py").write_text(
        "def accept(auction, bid_id, code_creator, mode):\n"
        "    return dict(code_creator=code_creator, mode=mode)\n",
        encoding="utf-8",
    )

    found = code_minting_call_sites(tmp_path)
    assert [site.path.name for site in found] == ["shortcut.py"]
    assert found[0].callee == "create_code"


def test_the_minting_lint_catches_the_getattr_alias_escape(tmp_path: Path) -> None:
    (tmp_path / "sneaky.py").write_text(
        'def mint(creator):\n    fn = getattr(creator, "create_code")\n    return fn("s", {})\n',
        encoding="utf-8",
    )
    found = code_minting_call_sites(tmp_path)
    assert [site.callee for site in found] == ["getattr(..., 'create_code')"]


# =====================================================================================
# Acceptance 2 — SimulatedRedirectProvider needs no Shopify; C11 parity across providers
# =====================================================================================
def test_the_simulated_provider_mints_a_single_use_code_and_an_on_domain_permalink() -> None:
    result = SimulatedRedirectProvider().checkout(request(mode="redirect"))

    assert result.code.startswith(CODE_PREFIX)
    assert len(result.code) == len(CODE_PREFIX) + 8
    assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN
    assert result.code in result.permalink_url
    # D22: the code dies with the offer, never after it.
    assert result.expires_at == code_expiry(T_NOW, offer())


def test_the_simulated_provider_needs_no_injected_creator_at_all() -> None:
    """ "No Shopify and no merchant app" has to mean the accept path runs without one."""
    result = SimulatedRedirectProvider().checkout(request(mode="redirect", creator=None))
    assert result.permalink_url


def test_a_minted_code_is_random_not_derived_from_the_offer() -> None:
    """A derivable single-use redeemable is a guessable one (D22)."""
    codes = {mint_code() for _ in range(64)}
    assert len(codes) == 64


def test_the_checkout_package_imports_nothing_shopify_or_merchant_shaped() -> None:
    """The simulated path must be reachable without the Shopify install lane existing."""
    imported: set[str] = set()
    for path in sorted(CHECKOUT_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

    forbidden = {"shopify", "shopify_stub", "services", "merchant", "merchant_svc", "httpx"}
    assert imported & forbidden == set(), f"the checkout port pulls in {imported & forbidden}"


def test_redirect_and_shopify_emit_the_identical_ordered_event_kinds() -> None:
    """C11: provider swap is invisible downstream."""
    redirect = resolve_provider("redirect").checkout(request(mode="redirect"))
    shopify = resolve_provider("shopify").checkout(
        request(mode="shopify", creator=RecordingCodeCreator())
    )
    stub = resolve_provider("shopify_stub").checkout(
        request(mode="shopify_stub", creator=RecordingCodeCreator())
    )

    assert redirect.kinds == shopify.kinds == stub.kinds == list(CHECKOUT_EVENT_KINDS)
    assert redirect.kinds == ["accepted", "code_created", "checkout_redirect"]
    assert redirect.provider != shopify.provider, "the two really are different providers"


def test_the_shopify_adapter_delegates_minting_to_the_injected_merchant_client() -> None:
    creator = RecordingCodeCreator()
    result = resolve_provider("shopify").checkout(request(mode="shopify", creator=creator))

    assert len(creator.calls) == 1
    assert creator.calls[0][0] == "store-a"
    assert result.code == creator.code
    assert creator.code in result.permalink_url


def test_the_simulated_provider_does_not_call_the_merchant_client_even_when_given_one() -> None:
    creator = RecordingCodeCreator()
    resolve_provider("redirect").checkout(request(mode="redirect", creator=creator))
    assert creator.calls == []


def test_the_shopify_adapter_refuses_to_run_without_its_merchant_client() -> None:
    """Falling back to a local mint would hand the buyer a code the merchant never issued."""
    with pytest.raises(CheckoutCreatorError):
        resolve_provider("shopify").checkout(request(mode="shopify", creator=None))


def test_a_creator_that_returns_no_code_is_an_error_not_a_silent_local_mint() -> None:
    class Empty:
        def create_code(self, store_id: str, offer: Any) -> dict[str, Any]:
            return {"permalink_url": f"https://{SELLER_DOMAIN}/cart/1:1"}

    with pytest.raises(CheckoutCreatorError):
        resolve_provider("shopify").checkout(request(mode="shopify", creator=Empty()))


# =====================================================================================
# Acceptance 3 — the port validates the host BEFORE any provider is asked to mint
# =====================================================================================
@pytest.mark.parametrize(("label", "url"), sorted(SPOOFS.items()))
@pytest.mark.parametrize("mode", list(CHECKOUT_MODES))
def test_an_off_domain_checkout_url_is_refused_with_no_code_created(
    label: str, url: str, mode: str
) -> None:
    creator = RecordingCodeCreator()
    with pytest.raises(OffDomainCheckout):
        resolve_provider(mode).checkout(request(mode=mode, checkout_url=url, creator=creator))
    assert creator.calls == [], f"{label}: a code was minted for an off-domain checkout"


def test_the_positive_control_an_on_domain_offer_completes() -> None:
    """Without this, a port that refused everything would satisfy every assertion above."""
    for mode in CHECKOUT_MODES:
        creator = RecordingCodeCreator()
        result = resolve_provider(mode).checkout(request(mode=mode, creator=creator))
        assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN
        assert result.code


def test_the_domain_check_runs_before_mint_is_ever_entered() -> None:
    """ "Before" is the assertion — a check after minting leaves a live code behind."""

    class Tattletale(CheckoutProvider):
        name = "tattletale"

        def __init__(self) -> None:
            self.mints = 0

        def mint(self, req: CheckoutRequest) -> MintedCheckout:
            self.mints += 1
            return MintedCheckout(
                code="X", permalink_url=f"https://{SELLER_DOMAIN}/cart/1:1?discount=X"
            )

    provider = Tattletale()
    with pytest.raises(OffDomainCheckout):
        provider.checkout(request(checkout_url=SPOOFS["userinfo spoof"]))
    assert provider.mints == 0

    provider.checkout(request())
    assert provider.mints == 1


def test_a_provider_that_returns_an_off_domain_permalink_is_refused_too() -> None:
    """The provider's own output is untrusted: a buggy adapter must not redirect the buyer."""

    class Wanderer(CheckoutProvider):
        name = "wanderer"

        def mint(self, req: CheckoutRequest) -> MintedCheckout:
            return MintedCheckout(
                code="X", permalink_url="https://attacker.tld/cart/1:1?discount=X"
            )

    with pytest.raises(OffDomainCheckout):
        Wanderer().checkout(request())


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<b>hi</b>",
        f"//{SELLER_DOMAIN}/cart/1:1",
        "",
        "not a url at all",
    ],
)
def test_a_checkout_url_that_is_not_a_browsable_seller_url_is_refused(url: str) -> None:
    assert is_on_domain(url, SELLER_DOMAIN) is False


def test_a_seller_with_no_registered_domain_cannot_be_checked_out_to() -> None:
    creator = RecordingCodeCreator()
    with pytest.raises(OffDomainCheckout):
        resolve_provider("shopify").checkout(request(mode="shopify", creator=creator, domain=""))
    assert creator.calls == []


def test_the_host_comparison_ignores_case_and_a_trailing_root_dot() -> None:
    assert is_on_domain(f"https://{SELLER_DOMAIN.upper()}/cart/1:1", SELLER_DOMAIN) is True
    assert is_on_domain(f"https://{SELLER_DOMAIN}./cart/1:1", SELLER_DOMAIN) is True


# =====================================================================================
# Acceptance 4 — the mode argument is the selector; a new provider adds no parameter
# =====================================================================================
def test_registering_a_further_provider_widens_nothing() -> None:
    """A new provider is one `register_provider` call and one `mint`. Nothing else moves."""

    class BankTransferProvider(CheckoutProvider):
        name = "bank-transfer"

        def mint(self, req: CheckoutRequest) -> MintedCheckout:
            return MintedCheckout(
                code="BT-1", permalink_url=f"https://{req.store_domain}/cart/1:1?discount=BT-1"
            )

    before = inspect.signature(CheckoutProvider.checkout)
    register_provider("bank_transfer", BankTransferProvider())
    try:
        assert inspect.signature(CheckoutProvider.checkout) == before
        assert list(before.parameters) == ["self", "request"]

        result = resolve_provider("bank_transfer").checkout(request(mode="bank_transfer"))
        # It is invisible downstream, which is the point of the port.
        assert result.kinds == list(CHECKOUT_EVENT_KINDS)
        assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN

        # And it inherits the domain check without opting in to it.
        with pytest.raises(OffDomainCheckout):
            resolve_provider("bank_transfer").checkout(
                request(mode="bank_transfer", checkout_url=SPOOFS["subdomain"])
            )
    finally:
        register_provider("bank_transfer", SimulatedRedirectProvider())

    # `accept()` is T-033's, and it exists: the mode it already receives is the selector, so
    # no provider registration may widen its four positionals.
    #
    # This import used to be wrapped in `try/except ImportError: accept = None` with the
    # assertion under `if accept is not None`, from when the module had not landed. That
    # tolerance outlived its reason and made the check silently self-disarming — ANY import
    # failure anywhere under `exchange.accept`, including a transitive one, turned the whole
    # assertion off and left the test green. The module is here now; the import is
    # unconditional, so a broken `accept` fails this test instead of hiding behind it.
    from exchange.accept import accept  # noqa: PLC0415

    positional = [
        name
        for name, parameter in inspect.signature(accept).parameters.items()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        and parameter.default is parameter.empty
    ]
    assert len(positional) == 4, f"accept() grew past four positionals: {positional}"


def test_a_provider_cannot_opt_out_of_the_ports_guarantees() -> None:
    """The seal is what makes the domain check and C11 parity true of providers unwritten."""
    with pytest.raises(PortMethodIsFinal):

        class OptOut(CheckoutProvider):
            name = "opt-out"

            def checkout(self, req: CheckoutRequest) -> Any:
                return {"permalink_url": "https://attacker.tld/cart/1:1?discount=X"}

    # The positive control: a well-behaved subclass is accepted.
    class WellBehaved(CheckoutProvider):
        name = "well-behaved"

        def mint(self, req: CheckoutRequest) -> MintedCheckout:
            return MintedCheckout(code="OK", permalink_url=f"https://{req.store_domain}/cart/1:1")

    assert WellBehaved().checkout(request()).code == "OK"


def test_an_unimplemented_provider_says_so_rather_than_returning_nothing() -> None:
    class Unfinished(CheckoutProvider):
        name = "unfinished"

    with pytest.raises(NotImplementedError):
        Unfinished().checkout(request())


def test_the_shopify_adapter_is_a_normal_provider_behind_the_same_port() -> None:
    """T-052 replaces what is behind the injected client; nothing about the port moves."""
    assert isinstance(ShopifyCheckoutProvider(), CheckoutProvider)
    assert ShopifyCheckoutProvider.checkout is CheckoutProvider.checkout


# =====================================================================================
# The trusted half of the host comparison must not come from the untrusted half's author
# =====================================================================================
class SellerDomains:
    """The platform's registered-domain lookup — ``app.sellers``, in process."""

    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = dict(rows)

    def domain_for(self, store_id: str) -> str | None:
        return self.rows.get(store_id)


SELLERS = SellerDomains({"store-a": SELLER_DOMAIN})


def hostile_request(
    *, domain: str, url: str, store_id: str = "store-a", sellers: Any = SELLERS, creator: Any = None
) -> CheckoutRequest:
    """A bid that writes BOTH halves of the comparison, as a store's own reply can."""
    return CheckoutRequest(
        auction_id="auction-1",
        bid_ref="bid-a",
        store_id=store_id,
        store_domain=domain,
        offer=offer(url),
        mode="redirect",
        code_creator=creator,
        now=T_NOW,
        registered_domains=sellers,
    )


@pytest.mark.parametrize("mode", list(CHECKOUT_MODES))
def test_a_store_cannot_supply_the_domain_it_is_checked_against(mode: str) -> None:
    """The exact-host check is only as good as the domain it is handed.

    ``bid["store_domain"]`` is a field in the store's own reply. A store that writes
    ``attacker.tld`` into it and ``https://attacker.tld/cart/...`` into ``checkout_url``
    agrees with itself, so the comparison passes and the buyer is redirected off-domain with
    a live discount code. Given the platform's lookup, the bid's claim is discarded.
    """
    creator = RecordingCodeCreator()
    request_ = hostile_request(
        domain="attacker.tld", url="https://attacker.tld/cart/1:1?discount=X", creator=creator
    )
    with pytest.raises(OffDomainCheckout):
        resolve_provider(mode).checkout(request_)
    assert creator.calls == [], "a code was minted for a domain the store named itself"


def test_the_platform_domain_wins_over_the_bids_claim_in_both_directions() -> None:
    """The positive control: the lookup is consulted, not merely used to refuse things."""
    creator = RecordingCodeCreator()
    result = resolve_provider("redirect").checkout(
        hostile_request(
            # The bid lies about its domain, but the URL is on the domain the PLATFORM holds.
            domain="attacker.tld",
            url=f"https://{SELLER_DOMAIN}/cart/1:1?discount=NET",
            creator=creator,
        )
    )
    assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN
    assert result.code


def test_an_unknown_seller_mints_nothing_rather_than_falling_back_to_the_bid() -> None:
    creator = RecordingCodeCreator()
    with pytest.raises(OffDomainCheckout):
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-ghost",
                store_domain="ghost.tld",
                offer=offer("https://ghost.tld/cart/1:1"),
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
                registered_domains=SELLERS,
            )
        )
    assert creator.calls == []


def test_a_registered_domain_lookup_that_raises_fails_closed() -> None:
    """Otherwise a store gets its own claim honoured by making the lookup fail."""

    class Down:
        def domain_for(self, store_id: str) -> str:
            raise RuntimeError("sellers table unreachable")

    creator = RecordingCodeCreator()
    with pytest.raises(OffDomainCheckout):
        resolve_provider("shopify").checkout(
            hostile_request(
                domain="attacker.tld",
                url="https://attacker.tld/cart/1:1",
                sellers=Down(),
                creator=creator,
            )
        )
    assert creator.calls == []


# =====================================================================================
# "No checkout_url" is not "off-domain": R10's list-price fallback must stay buyable
# =====================================================================================
def test_a_list_price_fallback_offer_can_be_checked_out(monkeypatch: Any) -> None:
    """The fallback offer the exchange builds for itself carries no checkout_url (R10).

    Refusing it as if it were a spoof made every Tier-0 and every silent store rankable but
    unbuyable — the entries T-030 manufactures could win an auction that then could not
    complete. The offer is built here by ``collect_bids`` itself, not hand-rolled, so this
    test breaks if that shape ever changes.
    """
    from exchange.auction import collect_bids  # noqa: PLC0415

    entry = collect_bids(
        [{"store_id": "store-a", "tier": 0, "product_ref": "product-1", "list_price": 160.0}],
        [],
        T_NOW,
    )[0]
    assert entry.fallback is True
    assert "checkout_url" not in entry.offer, "the fixture must be a real fallback offer"

    result = resolve_provider("redirect").checkout(
        CheckoutRequest(
            auction_id="auction-1",
            bid_ref="bid-a",
            store_id=entry.store_id,
            store_domain=SELLER_DOMAIN,
            offer=entry.offer,
            mode="redirect",
            now=T_NOW,
            registered_domains=SELLERS,
        )
    )
    assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN
    assert result.code.startswith(CODE_PREFIX)


def test_an_absent_checkout_url_still_cannot_reach_a_seller_with_no_registered_domain() -> None:
    """Relaxing "absent" must not relax "unknown seller" — the permalink check catches it."""
    creator = RecordingCodeCreator()
    with pytest.raises(OffDomainCheckout):
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain="",
                offer={"product_ref": "product-1", "unit_price": 100.0, "total_price": 100.0},
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
            )
        )
    assert creator.calls == []


# =====================================================================================
# A malformed offer field must be refused BEFORE the merchant issues a real code
# =====================================================================================
@pytest.mark.parametrize(
    "bad_field", [{"expires_at": "whenever"}, {"quantity": "lots"}, {"quantity": 0}]
)
def test_an_unusable_offer_field_is_refused_before_anything_is_minted(
    bad_field: dict[str, Any],
) -> None:
    """The failure used to land *after* ``POST /codes`` had issued a live single-use code.

    That leaves a real discount loose in the merchant's account with no ``code_created``
    event recorded for it — a discount the exchange cannot see, cannot expire and never
    agreed to. The port validates first, so nothing is minted at all.
    """
    creator = RecordingCodeCreator()
    hostile_offer = offer()
    hostile_offer.update(bad_field)

    with pytest.raises(UnusableOffer):
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=hostile_offer,
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
            )
        )
    assert creator.calls == [], "the merchant minted a code the exchange then threw away"


def test_the_positive_control_a_well_formed_quantity_and_expiry_still_complete() -> None:
    good = offer()
    good.update({"quantity": 3, "expires_at": T_FUTURE})
    result = resolve_provider("redirect").checkout(
        CheckoutRequest(
            auction_id="auction-1",
            bid_ref="bid-a",
            store_id="store-a",
            store_domain=SELLER_DOMAIN,
            offer=good,
            mode="redirect",
            now=T_NOW,
        )
    )
    assert ":3?" in result.permalink_url
    assert result.expires_at == code_expiry(T_NOW, good)


# =====================================================================================
# T-182: the schema types `Offer.expires_at` as an ISO-8601 STRING, and the mint refused it
# =====================================================================================
#: The instant every ISO spelling below names, and the epoch float that is the same instant.
#: Chosen to sit ~27.8 hours after ``T_NOW`` so it is INSIDE D22's 48-hour ceiling: with an
#: expiry past the ceiling, ``min()`` returns the ceiling whatever the parse did, and the
#: test could not tell a parsed expiry from a discarded one.
ISO_EXPIRY_EPOCH = 1_700_100_000.0

#: Every spelling `contracts.parse_timestamp` accepts, which is the set the boundary admits
#: at `packages/contracts/src/boundary.py` — so anything `validate_bid` lets through reaches
#: the mint. A naive instant is read as UTC on both sides, deliberately (a boundary that read
#: it as local time would make expiry depend on where the process happens to run).
ISO_EXPIRY_SPELLINGS = {
    "Z suffix": "2023-11-16T02:00:00Z",
    "explicit UTC offset": "2023-11-16T02:00:00+00:00",
    "naive, read as UTC": "2023-11-16T02:00:00",
    "non-UTC offset": "2023-11-15T18:00:00-08:00",
}


def test_the_schema_still_types_the_offer_expiry_as_a_string() -> None:
    """The premise of every test below. If contracts changes, these stop meaning anything."""
    from contracts import Offer  # noqa: PLC0415

    assert Offer.model_fields["expires_at"].annotation == (str | None), (
        "Offer.expires_at is no longer an ISO string; the ISO cases below now pin nothing"
    )


@pytest.mark.parametrize("spelling", sorted(ISO_EXPIRY_SPELLINGS), ids=str)
def test_an_iso_expiry_the_schema_permits_is_read_not_refused(spelling: str) -> None:
    """T-182: `float("2026-09-03T00:00:00Z")` raised, so EVERY schema-valid expiry was fatal.

    The schema types ``Offer.expires_at`` as ``str | None`` and the boundary parses it with
    ``contracts.parse_timestamp``; the mint parsed it with ``float()``. The only expiry shape
    the schema permitted was therefore the one the minting path refused, and the only one the
    mint accepted — a bare epoch number — is one the schema forbids.
    """
    assert code_expiry(T_NOW, {"expires_at": ISO_EXPIRY_SPELLINGS[spelling]}) == ISO_EXPIRY_EPOCH


def test_the_iso_and_epoch_spellings_of_one_instant_expire_at_the_same_second() -> None:
    """Widening must not have introduced a second answer: one instant, one expiry."""
    assert code_expiry(T_NOW, {"expires_at": "2023-11-16T02:00:00Z"}) == code_expiry(
        T_NOW, {"expires_at": ISO_EXPIRY_EPOCH}
    )


def test_the_forty_eight_hour_ceiling_still_caps_a_distant_iso_expiry() -> None:
    """D22 is ``min(now + 48h, offer.expires_at)`` — parsing the string must not lift the cap."""
    assert code_expiry(T_NOW, {"expires_at": "2033-05-18T03:33:20Z"}) == T_NOW + 48 * 60 * 60


@pytest.mark.parametrize("unreadable", ["whenever", "2026-13-45T99:99:99Z", "", [], {}])
def test_an_unreadable_expiry_is_still_refused_before_anything_is_minted(
    unreadable: Any,
) -> None:
    """The positive control. Accepting ISO must not mean accepting anything at all."""
    creator = RecordingCodeCreator()
    with pytest.raises(UnusableOffer):
        code_expiry(T_NOW, {"expires_at": unreadable})

    hostile = offer()
    hostile["expires_at"] = unreadable
    with pytest.raises(UnusableOffer):
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=hostile,
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
                registered_domains=SELLERS,
            )
        )
    assert creator.calls == [], "the merchant minted a code for an offer that cannot expire"


def test_a_whole_checkout_completes_on_an_iso_expiry() -> None:
    """End to end, through the port: the shape the schema publishes mints a real code."""
    hosted = offer()
    hosted["expires_at"] = "2023-11-16T02:00:00Z"
    result = resolve_provider("redirect").checkout(
        CheckoutRequest(
            auction_id="auction-1",
            bid_ref="bid-a",
            store_id="store-a",
            store_domain=SELLER_DOMAIN,
            offer=hosted,
            mode="redirect",
            now=T_NOW,
            registered_domains=SELLERS,
        )
    )
    assert result.code.startswith(CODE_PREFIX)
    assert result.expires_at == ISO_EXPIRY_EPOCH
    assert [event["kind"] for event in result.events] == list(CHECKOUT_EVENT_KINDS)


# =====================================================================================
# T-157: a code minted and then orphaned by the post-mint permalink check
# =====================================================================================
class OffDomainCodeCreator:
    """A merchant that issues a REAL code and answers with a permalink on another host."""

    def __init__(self, code: str = "PSX-REALCODE") -> None:
        self.code = code
        self.calls: list[tuple[str, Any]] = []

    def create_code(self, store_id: str, offer_payload: Any) -> dict[str, str]:
        self.calls.append((store_id, offer_payload))
        return {
            "code": self.code,
            "permalink_url": f"https://attacker.tld/cart/1:1?discount={self.code}",
        }


#: The R10 list-price fallback shape `collect_bids` manufactures: no `checkout_url` at all.
FALLBACK_OFFER: dict[str, Any] = {
    "product_ref": "product-1",
    "unit_price": 100.0,
    "total_price": 100.0,
}


def test_a_refusal_after_the_mint_carries_the_live_code_out() -> None:
    """T-157: the merchant issued ``PSX-REALCODE`` and this layer used to lose it entirely.

    With no ``checkout_url`` — the legal R10 fallback shape — the pre-mint host check has
    nothing to look at, so the first failable comparison is the one on the permalink the
    merchant returned, i.e. AFTER ``POST /codes`` issued a real single-use discount. The
    refusal is correct; dropping the code on the floor is not. A discount that exists in the
    merchant's system and nowhere in the exchange's cannot be revoked, cannot be expired and
    will not appear in any reconciliation.
    """
    creator = OffDomainCodeCreator()
    with pytest.raises(OffDomainCheckout) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=dict(FALLBACK_OFFER),
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
                registered_domains=SELLERS,
            )
        )

    assert creator.calls, "the fixture must actually reach the merchant, or it proves nothing"
    assert isinstance(raised.value, OrphanedCheckoutCode)
    orphan = raised.value.orphan
    assert orphan.code == "PSX-REALCODE"
    assert orphan.permalink_url == "https://attacker.tld/cart/1:1?discount=PSX-REALCODE"
    assert orphan.provider == "shopify"
    assert (orphan.store_id, orphan.auction_id, orphan.bid_ref) == (
        "store-a",
        "auction-1",
        "bid-a",
    )
    # T-215 inverted this assertion. It used to read `"PSX-REALCODE" in str(raised.value)`,
    # on the reasoning that "an operator reading the refusal cannot see there is a live code
    # to revoke" — a real requirement, met by the wrong mechanism. `accept()` formats this
    # message into a PERSISTED `policy_event` payload that the published OpenAPI types as a
    # bare string, so spelling the code in the prose published a live discount this layer
    # cannot revoke to anything holding the event stream. The requirement is kept and the
    # mechanism replaced: the message still says a code is live and must be revoked, and
    # names a fingerprint that joins to the `code_created` event holding the real code.
    message = str(raised.value)
    assert "PSX-REALCODE" not in message, (
        f"the live discount code is spelled in a message that gets persisted: {message!r}"
    )
    assert code_fingerprint("PSX-REALCODE") in message, (
        "an operator reading the refusal has no handle that joins to the recorded code"
    )
    assert "revoke" in message, "the refusal does not say a live code needs revoking"
    assert "attacker.tld" in message, "the refused host is the diagnostic; it must survive"


def test_a_refusal_before_the_mint_carries_no_code_because_there_is_none() -> None:
    """The positive control: the orphan marker must mean something, not ride every refusal.

    An off-domain ``checkout_url`` is refused by the PRE-mint check. Nothing was minted, so
    there is nothing to revoke — and an exception claiming otherwise would send the exchange
    hunting for a code that does not exist.
    """
    creator = OffDomainCodeCreator()
    with pytest.raises(OffDomainCheckout) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=offer("https://attacker.tld/cart/1:1?discount=X"),
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
                registered_domains=SELLERS,
            )
        )
    assert creator.calls == []
    assert not isinstance(raised.value, OrphanedCheckoutCode)


def test_the_orphan_refusal_is_still_caught_by_a_caller_watching_for_off_domain() -> None:
    """`accept()` and every existing caller catch `OffDomainCheckout`; that must keep working.

    Asserted by CATCHING, not by `issubclass`: the class statement and an `issubclass` of it
    are the same fact written twice, and a caller's `except` clause is the thing that has to
    keep working. This is the real post-mint refusal, caught the way `accept()` catches it.
    """
    creator = OffDomainCodeCreator()
    caught: Exception | None = None
    try:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=dict(FALLBACK_OFFER),
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
                registered_domains=SELLERS,
            )
        )
    except OffDomainCheckout as exc:  # the clause every existing caller already has
        caught = exc
    assert caught is not None, "an existing `except OffDomainCheckout` no longer catches this"
    assert isinstance(caught, ValueError), "and `accept()`'s broad catch still sees it"
    assert isinstance(caught, OrphanedCheckoutCode), "the orphan is reachable from that clause"


def test_the_simulated_provider_cannot_orphan_a_code() -> None:
    """It builds its permalink from the registered domain, so step 5 cannot fail on it."""
    result = resolve_provider("redirect").checkout(
        CheckoutRequest(
            auction_id="auction-1",
            bid_ref="bid-a",
            store_id="store-a",
            store_domain=SELLER_DOMAIN,
            offer=dict(FALLBACK_OFFER),
            mode="redirect",
            now=T_NOW,
            registered_domains=SELLERS,
        )
    )
    assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN


# =====================================================================================
# T-183: `contracts.Discount.value` is a PERCENT; Shopify's discount input is a FRACTION
# =====================================================================================
def test_a_protocol_discount_percent_becomes_a_shopify_fraction() -> None:
    """20.0 in the protocol means 20% off; 20.0 in Shopify's input would mean 2000% off.

    `services/shopify-stub/src/graphql_admin.py` refuses anything outside ``0.0..1.0``,
    faithfully to the real Admin API (the recorded fixture
    ``admin_discount_code_basic_create.json`` carries the sentence "Value must be between
    0.00 - 1.00" against the INPUT object). So the conversion is the exchange's to do, and
    this is the one place it happens.
    """
    assert shopify_discount_percentage({"type": "percentage", "value": 20.0}) == 0.2
    assert shopify_discount_percentage({"type": "percentage", "value": 0.0}) == 0.0
    assert shopify_discount_percentage({"type": "percentage", "value": 100.0}) == 1.0


def test_a_contracts_discount_object_converts_the_same_way_a_mapping_does() -> None:
    """The producer hands over a pydantic `Discount`, not a dict. Both must read alike."""
    from contracts import Discount  # noqa: PLC0415

    granted = Discount(type="percentage", value=20.0)
    assert shopify_discount_percentage(granted) == 0.2
    assert shopify_discount_percentage(granted.model_dump()) == 0.2


def test_a_fixed_amount_discount_has_no_percentage_to_send() -> None:
    """Shopify's input is a union: a fixed amount goes in `discountAmount`, not `percentage`."""
    assert shopify_discount_percentage({"type": "fixed_amount", "value": 15.0}) is None
    assert shopify_discount_percentage(None) is None
    assert offer_discount_percentage({"product_ref": "p", "unit_price": 1.0}) is None
    # The in-test positive control: without it, `return None` for EVERY input satisfies the
    # three assertions above and this test passes against an empty implementation.
    assert shopify_discount_percentage({"type": "percentage", "value": 15.0}) == 0.15


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "percentage", "value": 101.0},
        {"type": "percentage", "value": -1.0},
        {"type": "percentage", "value": "twenty"},
        {"type": "percentage", "value": None},
        {"type": "mystery-unit", "value": 20.0},
    ],
)
def test_a_discount_whose_unit_cannot_be_established_is_refused_not_guessed(bad: Any) -> None:
    """Fail closed. Guessing the unit is precisely the 100x mistake this exists to stop.

    ``{"type": "percentage", "value": 0.2}`` is deliberately NOT here: 0.2% is a legal, if
    stingy, discount, and there is no way to tell it apart from a fraction that arrived in
    the wrong unit. That ambiguity is why the conversion has one home instead of being
    open-coded at each call site.
    """
    with pytest.raises(UnusableDiscount):
        shopify_discount_percentage(bad)


def test_the_offer_level_reader_finds_the_discount_the_protocol_puts_on_the_offer() -> None:
    hosted = offer()
    hosted["discount"] = {"type": "percentage", "value": 25.0}
    assert offer_discount_percentage(hosted) == 0.25


class FlakyDomains:
    """A registry that answers once and fails afterwards — a dropped connection, in effect."""

    def __init__(self, domain: str = SELLER_DOMAIN) -> None:
        self.domain = domain
        self.calls = 0

    def domain_for(self, store_id: str) -> str | None:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("registry connection dropped")
        return self.domain


class CodeOnlyCreator:
    """A merchant that answers with the code and no permalink — the adapter builds one."""

    def __init__(self, code: str = "PSX-CODEONLY") -> None:
        self.code = code
        self.calls: list[tuple[str, Any]] = []

    def create_code(self, store_id: str, offer_payload: Any) -> dict[str, str]:
        self.calls.append((store_id, offer_payload))
        return {"code": self.code}


def test_a_failure_inside_the_adapter_after_the_merchant_answered_carries_the_code_out() -> None:
    """The second window on T-157, one level deeper than the port can reach.

    ``CheckoutProvider.checkout`` can only guard what happens *after* ``mint`` returns. The
    Shopify adapter keeps working after the merchant has answered — when the merchant sends
    no permalink it builds one, and building one resolves the registered domain a SECOND
    time. A lookup that answers once and fails once is enough to raise there, with a real
    code already issued, and the port's wrapper would never see it.
    """
    creator = CodeOnlyCreator()
    registry = FlakyDomains()
    with pytest.raises(OrphanedCheckoutCode) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=dict(FALLBACK_OFFER),
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
                registered_domains=registry,
            )
        )

    assert creator.calls, "the merchant must actually have been reached"
    assert registry.calls > 1, "the second lookup must actually have happened"
    assert raised.value.orphan.code == "PSX-CODEONLY"
    assert raised.value.orphan.store_id == "store-a"


def test_the_positive_control_a_code_only_merchant_reply_still_completes_normally() -> None:
    """The refusal above must not be satisfiable by an adapter that refuses code-only replies."""
    creator = CodeOnlyCreator()
    result = resolve_provider("shopify").checkout(
        CheckoutRequest(
            auction_id="auction-1",
            bid_ref="bid-a",
            store_id="store-a",
            store_domain=SELLER_DOMAIN,
            offer=dict(FALLBACK_OFFER),
            mode="shopify",
            code_creator=creator,
            now=T_NOW,
            registered_domains=SELLERS,
        )
    )
    assert result.code == "PSX-CODEONLY"
    assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN


def test_an_orphan_refusal_survives_being_serialised() -> None:
    """The code is the payload. An exception that loses it in transit has lost the code.

    `BaseException.__reduce__` rebuilds by calling the class with `self.args`, and `orphan`
    is keyword-only — so without `__reduce__` this raises `TypeError` on the way back and
    turns a recoverable refusal into a crash wherever the exception crossed a boundary.
    """
    import pickle  # noqa: PLC0415

    original = OrphanedOffDomainCheckout(
        "refused",
        orphan=OrphanedCode(
            code="PSX-ROUNDTRIP",
            permalink_url="https://attacker.tld/cart/1:1",
            provider="shopify",
            store_id="store-a",
            auction_id="auction-1",
            bid_ref="bid-a",
        ),
    )
    revived = pickle.loads(pickle.dumps(original))
    assert isinstance(revived, OrphanedOffDomainCheckout)
    assert isinstance(revived, OffDomainCheckout)
    assert revived.orphan == original.orphan
    assert str(revived) == "refused"


class HostileReply:
    """A merchant reply whose second read raises — the handler must not read it again."""

    def __init__(self, code: str = "PSX-HOSTILE1") -> None:
        self.code = code
        self.reads = 0

    def get(self, key: str) -> Any:
        self.reads += 1
        if key == "code":
            return self.code
        raise RuntimeError("reply object exploded on the second read")


class HostileReplyCreator:
    def __init__(self) -> None:
        self.reply = HostileReply()
        self.calls: list[tuple[str, Any]] = []

    def create_code(self, store_id: str, offer_payload: Any) -> HostileReply:
        self.calls.append((store_id, offer_payload))
        return self.reply


def test_a_reply_that_raises_while_being_read_still_yields_its_code() -> None:
    """An exception raised *inside* the orphan handler would lose the orphan again.

    The handler therefore reads the permalink off a local, never back off the reply object.
    The code is what has to be revoked; the permalink is a nicety.
    """
    creator = HostileReplyCreator()
    with pytest.raises(OrphanedCheckoutCode) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=dict(FALLBACK_OFFER),
                mode="shopify",
                code_creator=creator,
                now=T_NOW,
                registered_domains=SELLERS,
            )
        )
    assert raised.value.orphan.code == "PSX-HOSTILE1"


#: ISO 8601 **basic** format — no separators. `datetime.fromisoformat` accepts it, so
#: `contracts.parse_timestamp` does too, so `validate_bid` admits a bid carrying it. Each of
#: these is ALSO a valid float, and reading it as one lands decades away from the instant the
#: boundary read: 20260903 seconds after the epoch is 23 August 1970.
BASIC_FORMAT_EXPIRIES = {
    "20260903": "2026-09-03T00:00:00Z",
    "19700102": "1970-01-02T00:00:00Z",
    "20231116": "2023-11-16T00:00:00Z",
}


@pytest.mark.parametrize("basic", sorted(BASIC_FORMAT_EXPIRIES))
def test_the_mint_reads_a_basic_format_expiry_as_the_boundary_reads_it(basic: str) -> None:
    """The two-parser hazard, made concrete — and the reason `float()` is not tried first.

    ``"20260903"`` is both a valid ISO 8601 basic-format date AND a valid float. The boundary
    admits the bid on the first reading; a mint that took the second would issue a code that
    expired in 1970 for an offer everything upstream agreed was live. There must be exactly
    one answer, and it must be the boundary's.
    """
    from contracts.boundary import parse_timestamp  # noqa: PLC0415

    boundary = parse_timestamp(basic)
    assert boundary is not None, "the premise: the boundary admits this spelling"

    minted = code_expiry(T_NOW, {"expires_at": basic})
    assert minted == min(T_NOW + 48 * 60 * 60, boundary.timestamp()), (
        "the mint did not read the instant the boundary read"
    )
    assert minted == code_expiry(T_NOW, {"expires_at": BASIC_FORMAT_EXPIRIES[basic]}), (
        "the basic and extended spellings of one date must expire at the same second"
    )
    assert minted != float(basic), "the expiry was read as an epoch second, not as a date"


@pytest.mark.parametrize("numeric_string", ["1700100000", "1700100000.0", " 1700100000 "])
def test_a_numeric_string_expiry_still_reads_as_an_epoch(numeric_string: str) -> None:
    """`parse_timestamp` refuses these, so the `float()` fallback must still be reachable.

    The boundary would reject a bid spelling its expiry this way, but the port is also driven
    directly — by `collect_bids`' fallback offers and by callers that never went through
    `validate_bid` — and this shape worked here before T-182. It is a fallback, not a first
    choice: see the basic-format test above for what happens when it goes first.
    """
    assert code_expiry(T_NOW, {"expires_at": numeric_string}) == 1_700_100_000.0


# =====================================================================================
# T-345 — the money path ACCEPTED malformed input instead of refusing it
#
# The class the denial-leak sweep structurally could not see, and said so: a case that
# ACCEPTS has no denial reason to assert on, so every sink assertion in that sweep is
# vacuous for it. Both halves were measured live at HEAD through `accept()`:
#
#   HALF 1 (the code)   creator -> {"code": object()}
#       accepted=True  denial_reason=None  code='<object object at 0x100c312f0>'
#       code_created payload={'code': '<object object at 0x100c312f0>', ...}
#       permalink       '…/cart/1:1?discount=%3Cobject%20object%20at%200x100c312f0%3E'
#     — a LIVE single-use discount whose spelling is this process's memory layout, handed
#     to a buyer in a query string and written into a persisted event.
#
#   HALF 2 (the money)  offer -> {"unit_price": object()}
#       accepted=True  denial_reason=None
#       accepted payload={'offer': {'unit_price': <object object at 0x100c31350>, …}}
#     — reachable only on the published four-positional `accept()` surface, which skips
#     collection; the SERVED path already refused it in `auction/collect.py`.
#
# `str()` never fails, so "is this a code?" and "is this a price?" were questions nothing
# asked. Both are asked now, at the contracts boundary, and each gate below is paired with
# a positive control — a wall that refuses everything satisfies every refusal assertion.
# =====================================================================================
#: Codes a merchant may legitimately answer with. NONE of them is D22's own shape, and that
#: is the point: the merchant mints in its own vocabulary, and a door that pinned a shape
#: here would refuse the real Shopify adapter's answers. All-numeric, lower case, no `PSX-`
#: prefix, one character, and the longest the boundary admits.
HONEST_MERCHANT_CODES = ["90210", "spring-sale", "psx-lower-9021", "a", "0" * 128]

#: Values that are not a code, in the ways that matter differently. `object()` is the
#: ticket's own reproduction; the mapping is the merchant's WHOLE REPLY handed in where its
#: `code` field belonged, which stringified would mint a "code" made of braces and quotes.
UNREADABLE_MERCHANT_CODES = {
    "a bare object": object(),
    "bytes": b"PSX-BYTES-01",
    "an int": 90210,
    "a float": 1.5,
    "a bool": True,
    "the whole reply": {"code": "PSX-NESTED-1"},
    "a list": ["PSX-LIST-01"],
    "blank": "   ",
    "a NUL inside": "PSX-\x00-01",
    "a C1 control inside": "PSX-\x85-01",
    "a non-breaking space inside": "PSX-\xa0-01",
    "longer than the boundary admits": "P" * 129,
}


class JunkCodeCreator:
    """A merchant ``POST /codes`` client that mints, then describes its code unreadably."""

    def __init__(self, code: Any) -> None:
        self.code = code
        self.calls: list[tuple[str, Any]] = []

    def create_code(self, store_id: str, offer_payload: Any) -> dict[str, Any]:
        self.calls.append((store_id, offer_payload))
        return {"code": self.code}

    __call__ = create_code


def shopify_checkout(creator: Any, offer_payload: dict[str, Any] | None = None) -> Any:
    """One Shopify checkout through the port, on the domain, with nothing else wrong."""
    return resolve_provider("shopify").checkout(
        CheckoutRequest(
            auction_id="auction-1",
            bid_ref="bid-a",
            store_id="store-a",
            store_domain=SELLER_DOMAIN,
            offer=offer_payload if offer_payload is not None else offer(),
            mode="shopify",
            code_creator=creator,
            now=T_NOW,
        )
    )


# --- the arming tests. Every gate below is worthless if the boundary answers nothing ---
def test_the_mint_asks_the_boundary_a_question_it_can_actually_answer() -> None:
    """ARMING, not xfail. Four ways the code gates below could pass while measuring nothing.

    Each is a way the adoption could be present and inert: a boundary that refuses every
    value (so the positive controls are the real test), one that refuses none (so the
    refusals are), or one whose reason vocabulary has moved out from under the adapter.
    """
    from contracts.boundary import discount_code_reasons, minted_code_reasons  # noqa: PLC0415

    assert discount_code_reasons(object()) == ["code_unusable:code:not_a_string"], (
        "the boundary no longer refuses T-345's own reproduction value; every refusal "
        "assertion below now passes for some other reason, or not at all"
    )
    assert minted_code_reasons({"code": object()}) == ["code_unusable:code:not_a_string"], (
        "the reply-level door no longer answers about the ticket's measured reply shape"
    )
    assert minted_code_reasons("not a reply at all") == ["code_unusable:code:unreadable_reply"], (
        "the boundary no longer distinguishes a reply with no code from a non-reply"
    )
    for honest in HONEST_MERCHANT_CODES:
        assert discount_code_reasons(honest) == [], (
            f"the boundary refuses {honest!r}, an ordinary merchant code — the positive "
            f"controls below would then be pinning a door that is closed, not open"
        )


def test_the_mint_reads_the_boundarys_own_unreadable_price_verdict() -> None:
    """ARMING for the price half, and the pin on the one leaf spelling `codes.py` names.

    ``price_reasons`` answers a whole price walk and most of it is about a roster the mint
    does not hold, so ``assert_offer_is_mintable`` acts on two leaf verdicts by name. They
    are leaf strings rather than published constants, so this is what makes a rename in the
    boundary a red test here rather than a gate that quietly stops refusing anything.
    """
    from contracts.boundary import price_reasons  # noqa: PLC0415

    unreadable = {**offer(), "unit_price": object()}
    assert "price_unreconcilable:offer.unit_price:not_a_number" in price_reasons(
        {"offer": unreadable}
    ), "the boundary's unreadable-price verdict has moved; codes.py acts on the old spelling"

    negative = {**offer(), "unit_price": -1.0}
    assert "price_unreconcilable:offer.unit_price:negative" in price_reasons({"offer": negative}), (
        "the boundary's negative-price verdict has moved; codes.py acts on the old spelling"
    )


# --- HALF 1: the code -----------------------------------------------------------------
@pytest.mark.parametrize("label", sorted(UNREADABLE_MERCHANT_CODES))
def test_a_merchant_code_the_exchange_cannot_read_is_refused_not_coerced(label: str) -> None:
    """The reproduction. ``str()`` succeeds on everything, which is why nothing asked."""
    creator = JunkCodeCreator(UNREADABLE_MERCHANT_CODES[label])
    with pytest.raises(CheckoutCreatorError) as raised:
        shopify_checkout(creator)

    assert creator.calls, f"[{label}] the merchant must actually have been reached"
    assert "code_unusable:" in str(raised.value), (
        f"[{label}] the refusal does not carry the boundary's machine-readable verdict: "
        f"{raised.value}"
    )


@pytest.mark.parametrize("label", sorted(UNREADABLE_MERCHANT_CODES))
def test_an_unreadable_code_is_refused_BEFORE_it_is_coerced(label: str) -> None:
    """The ordering IS the fix, and the minting ledger is where it is observable.

    ``str(code)`` is the manufacture being refused, and the first thing the adapter used to
    do with the merchant's answer was report ``str(code)`` into the port's minting ledger —
    so one call published the address into the very channel a failure is read out of. An
    empty ledger is the assertion that no coercion happened anywhere.
    """
    creator = JunkCodeCreator(UNREADABLE_MERCHANT_CODES[label])
    with minting_ledger() as recorded:
        with pytest.raises(CheckoutCreatorError):
            shopify_checkout(creator)

    assert recorded == [], (
        f"[{label}] the unreadable value was coerced and reported as a minted code: {recorded!r}"
    )


@pytest.mark.parametrize("label", sorted(UNREADABLE_MERCHANT_CODES))
def test_the_refusal_publishes_no_memory_address(label: str) -> None:
    """T-264's rule on T-345's path: the refused VALUE is the one thing that may not be said.

    This sentence becomes ``denial_reason``, which ``_denied`` republishes verbatim in the
    409 and ``_refusal_event`` persists. The value being complained about may itself be a
    live discount, so the diagnostic is the reply's keys, its type, the boundary's reasons
    and the fingerprint — never the value.
    """
    creator = JunkCodeCreator(UNREADABLE_MERCHANT_CODES[label])
    with pytest.raises(CheckoutCreatorError) as raised:
        shopify_checkout(creator)

    text = str(raised.value)
    assert not re.search(r"0x[0-9a-fA-F]{6,}", text), (
        f"[{label}] the refusal published a memory address: {text!r}"
    )
    assert "PSX-NESTED-1" not in text and "PSX-LIST-01" not in text, (
        f"[{label}] the refusal quoted the merchant's own value back: {text!r}"
    )


def test_a_refused_unreadable_code_is_recorded_rather_than_dropped() -> None:
    """The consequence half. A merchant that answered has probably already minted.

    Refusing the description does not un-mint the discount, and this refusal cannot carry
    the code out the way an ``OrphanedCheckoutCode`` does — ``OrphanedCode.code`` is a
    ``str``, and the only string available is the coercion being refused. So the report goes
    down the channel built for a record that cannot be made: ids, key NAMES and the
    fingerprint, never a payload value.
    """
    from exchange.auction.ledger import audit_anomalies  # noqa: PLC0415

    seen_before = len(audit_anomalies())
    creator = JunkCodeCreator(object())
    with pytest.raises(CheckoutCreatorError) as raised:
        shopify_checkout(creator)

    recorded = audit_anomalies()[seen_before:]
    assert len(recorded) == 1, (
        "a merchant may be holding a live discount this exchange cannot name, and nothing "
        f"was recorded about it; anomalies since the call were {recorded!r}"
    )
    anomaly = recorded[0]
    assert anomaly.kind == "code_created", (
        "the record that could not be made is the code_created one; the anomaly must say so"
    )
    assert anomaly.where == "ShopifyCheckoutProvider.mint"
    assert anomaly.context["store_id"] == "store-a"
    assert anomaly.context["bid_ref"] == "bid-a"
    assert anomaly.context["auction_id"] == "auction-1"
    assert anomaly.context["code_reasons"] == ("code_unusable:code:not_a_string",)

    rendered = f"{anomaly.problem} {anomaly.context!r}"
    assert not re.search(r"0x[0-9a-fA-F]{6,}", rendered), (
        f"the anomaly published the value it exists to complain about: {rendered!r}"
    )
    assert anomaly.context["fingerprint"] in str(raised.value), (
        "the refusal and the anomaly do not share a join key, so an operator holding the "
        "client-visible reason cannot find the record of the possibly-live discount"
    )


@pytest.mark.parametrize("honest", HONEST_MERCHANT_CODES)
def test_the_positive_control_an_ordinary_merchant_code_still_mints(honest: str) -> None:
    """A gate that refused every merchant answer would satisfy every assertion above.

    None of these is D22's shape and none of them needs to be: the merchant mints in its own
    vocabulary, and pinning a shape here would refuse the real Shopify adapter's answers.
    """
    creator = JunkCodeCreator(honest)
    result = shopify_checkout(creator)
    assert result.code == honest
    assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN


def test_a_code_a_merchant_may_really_have_issued_still_reaches_the_orphan_machinery() -> None:
    """The carve-out, and the reason it is not a hole — measured, not asserted by fiat.

    ``code_unusable:code:whitespace`` is the one boundary verdict this adapter does not act
    on alone. A refusal here can never carry its code out, so every code this gate refuses
    becomes unrecordable and unrevokable — the right price for a value that is not a code,
    and the wrong one for a string the merchant really wrote, which records perfectly well.
    ``test_orphaned_code.py::…[plus-encoded-permalink]`` is that case live: a real
    ``'PSX LIVE 5'`` that must reach the port's host check and come back as a revocable
    orphan (T-202). What is KEPT is the half that truncates a code downstream.
    """
    minted = shopify_checkout(JunkCodeCreator("PSX LIVE 5"))
    assert minted.code == "PSX LIVE 5", "a printable code with a space must still mint"
    assert "discount=PSX%20LIVE%205" in minted.permalink_url, (
        "the code must be carried into the permalink verbatim and encoded, never trimmed"
    )

    for truncating in ("PSX-1\n", "PSX\t1", "PSX\x0b1", "PSX\xa01"):
        with pytest.raises(CheckoutCreatorError):
            shopify_checkout(JunkCodeCreator(truncating))


# --- HALF 2: the money ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "unit_price"),
    sorted(
        {
            "a bare object": object(),
            "a string a store wrote": "cheap",
            "a numeric string": "100.00",
            "a bool": True,
            "NaN": float("nan"),
            "infinity": float("inf"),
            "negative": -1.0,
        }.items()
    ),
)
def test_an_unreadable_unit_price_is_refused_before_anything_is_minted(
    label: str, unit_price: Any
) -> None:
    """The direct ``accept()`` surface now refuses what the served path already refused.

    ``auction/collect.py``'s ``_price_is_unreadable`` asks this of every bid it collects,
    deliberately with no roster term in it. The published four-positional ``accept()``
    skips collection entirely, so the wall existed on one route to the mint and not the
    other — and a bid whose ``unit_price`` was an ``object()`` was ACCEPTED, with a real
    discount minted for it and the object's address written into the persisted event.
    """
    creator = RecordingCodeCreator()
    with pytest.raises(UnusableOffer) as raised:
        shopify_checkout(creator, {**offer(), "unit_price": unit_price})

    assert creator.calls == [], (
        f"[{label}] the merchant minted a live code for an offer with no readable price"
    )
    assert "price_unreconcilable:offer.unit_price:" in str(raised.value), (
        f"[{label}] the refusal does not carry the boundary's verdict: {raised.value}"
    )
    assert not re.search(r"0x[0-9a-fA-F]{6,}", str(raised.value)), (
        f"[{label}] the refusal published the address of the value it refused"
    )


@pytest.mark.parametrize("honest", [100.0, 0.01, 12, 1e9, 0.0])
def test_the_positive_control_an_honest_price_still_completes(honest: Any) -> None:
    """A gate that refused every price would satisfy every assertion above.

    ``0.0`` is deliberately here: a free item is the ROSTER's refusal to make
    (``price_unreconcilable:offer.unit_price:not_positive``, which needs a catalog this
    module does not hold and is not entitled to ask for). The mint judges readability only.
    """
    result = shopify_checkout(RecordingCodeCreator(), {**offer(), "unit_price": honest})
    assert result.code
    assert urlsplit(result.permalink_url).hostname == SELLER_DOMAIN


def test_an_offer_that_states_no_price_at_all_is_not_this_gates_subject() -> None:
    """T-345 is a PRESENT malformed argument, and says so — it is not T-306/T-307.

    The R10 list-price fallback and every direct caller of the port are entitled to hand it
    an offer with no price on it, and ``price_reasons`` says ``not_a_number`` about an
    absent field exactly as it does about an ``object()``. Acting on that would refuse the
    required starting slice, so the gate reads the field only when the offer states one.
    """
    priceless = {k: v for k, v in offer().items() if k != "unit_price"}
    assert "unit_price" not in priceless
    assert shopify_checkout(RecordingCodeCreator(), priceless).code

    result = resolve_provider("redirect").checkout(
        CheckoutRequest(
            auction_id="auction-1",
            bid_ref="bid-a",
            store_id="store-a",
            store_domain=SELLER_DOMAIN,
            offer={},
            mode="redirect",
            now=T_NOW,
        )
    )
    assert result.code.startswith(CODE_PREFIX)
