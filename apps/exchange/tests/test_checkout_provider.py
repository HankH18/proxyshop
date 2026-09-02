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
    PortMethodIsFinal,
    ShopifyCheckoutProvider,
    SimulatedRedirectProvider,
    UnknownCheckoutMode,
    code_expiry,
    code_minting_call_sites,
    is_on_domain,
    mint_code,
    register_provider,
    registered_modes,
    resolve_provider,
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

    # `accept()` is T-033's. The moment it exists, this becomes binding: the mode it already
    # receives is the selector, so no provider registration may widen its four positionals.
    try:
        from exchange.accept import accept  # noqa: PLC0415
    except ImportError:
        accept = None  # type: ignore[assignment]
    if accept is not None:
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
