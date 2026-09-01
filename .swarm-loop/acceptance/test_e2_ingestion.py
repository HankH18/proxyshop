"""Epic E2 — Ingestion. FROZEN acceptance criteria.

Covers the promised behaviour of `services/ingest`: the SSRF-guarded, robots-respecting
signed fetcher (T-020, C10/C6), differential re-extraction and provenance-tagged claim
extraction with quarantine of low-confidence output (T-021, R8/C6/C10), entity resolution
into `SAME_AS` edges (T-022, DESIGN §Data models), and the single `CatalogAdapter`
interface both adapters satisfy (T-023, C6).

Authoring rules (see README.md in this directory, both are binding):
  1. Every import of product code happens INSIDE a test function, never at module scope.
  2. Every test carries @pytest.mark.epic("E2") and @pytest.mark.ticket("T-0xx").

Hermetic by construction: no datastore, no container, no network, no wall clock, no
project fixture. DNS is monkeypatched in the fetch-guard tests so the suite behaves
identically whether or not the machine has a resolver.
"""
from __future__ import annotations

import re

import pytest

_MISSING = object()

# The operations `CatalogAdapter` exists to abstract, taken from the two tickets that
# own it: T-020 acceptance 1 ("adapter ingests stub storefront fixture incl. password
# mode") and T-023 acceptance 2 ("mapping to graph upserts matches signed_fetch
# semantics"). Both adapters implement both; anything else the interface declares is the
# implementation's business, but these two are the seam DESIGN §Architecture names.
_CATALOG_ADAPTER_METHODS = ("fetch_catalog", "to_upserts")


def _field(obj, name, default=_MISSING):
    """Read `name` off a mapping or an object, failing loudly when it is absent.

    The product is free to return dataclasses, pydantic models or plain dicts; the frozen
    goal is the field, not the container.
    """
    if isinstance(obj, dict):
        value = obj.get(name, _MISSING)
    else:
        value = getattr(obj, name, _MISSING)
    if value is _MISSING:
        if default is _MISSING:
            raise AssertionError(f"expected a {name!r} field on {obj!r}")
        return default
    return value


def _plain(value):
    """Unwrap an enum-ish value to the primitive the SPEC names."""
    return getattr(value, "value", value)


def _install_fake_dns(monkeypatch, default_ip: str) -> None:
    """Resolve every non-literal hostname to `default_ip`; leave IP literals alone.

    Keeps the SSRF tests offline and deterministic no matter whether the implementation
    resolves before checking (which a correct SSRF guard must, to defeat rebinding).
    """
    import ipaddress
    import socket

    def _resolve(host: str) -> str:
        bare = str(host).strip("[]")
        try:
            ipaddress.ip_address(bare)
        except ValueError:
            return default_ip
        return bare

    def _getaddrinfo(host, port=None, *args, **kwargs):
        ip = _resolve(host)
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, int(port or 0)))]

    def _gethostbyname(host):
        ip = _resolve(host)
        if ":" in ip:
            raise OSError("no A record")
        return ip

    def _gethostbyname_ex(host):
        return (str(host), [], [_gethostbyname(host)])

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", _gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", _gethostbyname_ex)


def _forbid_network(monkeypatch) -> None:
    """Make any attempt to open a socket an immediate, obvious failure."""
    import socket

    def _boom(*args, **kwargs):
        raise AssertionError("this code path must not open a network connection")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


def _scraped_source():
    """A Source/Provenance descriptor for a fetched policy page.

    Prefers the pinned `packages.contracts.Provenance` (DESIGN: Source ≡ Provenance) and
    falls back to the equivalent mapping so the extraction goal does not depend on the
    order in which T-010 and T-021 land.
    """
    payload = {
        "source": "scraped",
        "source_class": "scraped",
        "source_id": "src-e2-shipping-0001",
        "url": "https://store.example.com/policies/shipping",
        "ref": "snapshot://store.example.com/policies/shipping@sha256:0f1e2d3c",
        "content_hash": "sha256:0f1e2d3c",
        "observed_at": "2026-01-01T00:00:00Z",
        "extractor_version": "acceptance-fixture",
        "authority_rank": 1,
    }
    try:
        from packages.contracts import Provenance
    except Exception:
        return payload
    try:
        return Provenance(
            source="scraped",
            ref=payload["ref"],
            observed_at=payload["observed_at"],
            authority_rank=1,
        )
    except Exception:
        return payload


POLICY_PAGE_TEXT = (
    "Shipping & Returns\n"
    "We ship every order within 2 business days from our Portland warehouse.\n"
    "Standard shipping is free on orders over $50; expedited shipping costs $12.\n"
    "Returns are accepted within 30 days of delivery for unworn items.\n"
    "All footwear carries a 1 year manufacturing guarantee.\n"
    "We may occasionally be able to ship faster during quiet weeks.\n"
)


def _upserted(result):
    """The claims an extraction run offers for upsert."""
    for name in ("claims", "upserts", "upserted"):
        value = _field(result, name, None)
        if value is not None:
            return list(value)
    if isinstance(result, (list, tuple)):
        return list(result)
    raise AssertionError(
        f"extract_claims must expose the upsert batch (a `claims` collection); got {result!r}"
    )


def _quarantined(result):
    """The claims an extraction run held back below the confidence floor."""
    for name in ("quarantined", "quarantine"):
        value = _field(result, name, None)
        if value is not None:
            return list(value)
    raise AssertionError(
        "extract_claims must expose a `quarantined` collection (C10: low-confidence "
        f"extraction is quarantined, not upserted); got {result!r}"
    )


def _confidence(claim) -> float:
    value = _field(claim, "confidence", None)
    if value is None:
        provenance = _field(claim, "provenance", None) or _field(claim, "source", None)
        if provenance is not None and not isinstance(provenance, str):
            value = _field(provenance, "confidence", None)
    if value is None:
        raise AssertionError(f"claim carries no extraction confidence: {claim!r}")
    return float(value)


def _adapter_class(obj, label: str):
    """Resolve an adapter name to its class, whether it is exported as a class or module."""
    import inspect

    if inspect.isclass(obj):
        return obj
    candidates = [
        value
        for name, value in vars(obj).items()
        if inspect.isclass(value) and not name.startswith("_") and "adapter" in name.lower()
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise AssertionError(
        f"{label} must be a CatalogAdapter implementation class (or a module exporting "
        f"exactly one *Adapter class); resolved {obj!r} with candidates {candidates!r}"
    )


def _public_methods(cls) -> dict:
    import inspect

    out = {}
    for name, value in inspect.getmembers(cls, predicate=callable):
        if name.startswith("_"):
            continue
        if getattr(value, "__objclass__", None) is object:
            continue
        out[name] = value
    return out


# ---------------------------------------------------------------------------------
# T-020 — signed fetch adapter: SSRF guard, redirect bounds, robots, identified UA
# ---------------------------------------------------------------------------------


@pytest.mark.epic("E2")
@pytest.mark.ticket("T-020")
def test_fetcher_refuses_private_and_link_local_hosts():
    """C10: the crawler is SSRF-guarded — private and link-local targets are refused."""
    from services.ingest.src.adapters import is_fetch_allowed

    with pytest.MonkeyPatch.context() as mp:
        _install_fake_dns(mp, "93.184.216.34")  # a public literal
        _forbid_network(mp)

        # Positive control: without this, a guard that refuses everything would "pass".
        assert is_fetch_allowed("https://store.example.com/products.json") is True

        refused = [
            "http://127.0.0.1/",
            "http://127.0.0.1:8080/admin",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://localhost:5432/",
            "http://0.0.0.0/",
        ]
        for url in refused:
            assert is_fetch_allowed(url) is False, f"SSRF guard admitted {url}"

    with pytest.MonkeyPatch.context() as mp:
        # DNS rebinding: a perfectly ordinary hostname that resolves into RFC1918 space
        # must be refused, which means the guard resolves before it decides.
        _install_fake_dns(mp, "10.0.0.5")
        _forbid_network(mp)
        assert is_fetch_allowed("https://looks-fine.example.net/products.json") is False


@pytest.mark.epic("E2")
@pytest.mark.ticket("T-020")
def test_redirects_are_bounded_to_allowed_hosts():
    """C10: redirect chains are bounded in length and may not leave the allow-listed hosts."""
    from services.ingest.src.adapters import is_redirect_chain_allowed

    allowed = ["store.example.com"]
    start = "https://store.example.com/products.json"

    with pytest.MonkeyPatch.context() as mp:
        _install_fake_dns(mp, "93.184.216.34")
        _forbid_network(mp)

        # Positive control: a short, on-host chain is fine.
        assert (
            is_redirect_chain_allowed(
                [start, "https://store.example.com/products.json?page=2"], allowed, 5
            )
            is True
        )

        # Leaves the allow-list.
        assert (
            is_redirect_chain_allowed([start, "https://attacker.tld/collect"], allowed, 5)
            is False
        )
        # Domain spoofing. The first defeats an allow-list matched by substring or
        # `startswith`; the second defeats one matched by a bare `endswith`, which admits
        # any sibling label. Neither is a subdomain of the allowed host, so refusing them
        # is required whether or not the implementation permits subdomains.
        assert (
            is_redirect_chain_allowed(
                [start, "https://store.example.com.attacker.tld/collect"], allowed, 5
            )
            is False
        )
        assert (
            is_redirect_chain_allowed(
                [start, "https://evilstore.example.com/collect"], allowed, 5
            )
            is False
        )
        # Redirect into link-local metadata.
        assert (
            is_redirect_chain_allowed(
                [start, "http://169.254.169.254/latest/meta-data/"], allowed, 5
            )
            is False
        )
        # Over the configured bound, every hop on an allowed host.
        long_chain = [start] + [
            f"https://store.example.com/hop/{i}" for i in range(8)
        ]
        assert is_redirect_chain_allowed(long_chain, allowed, 3) is False


@pytest.mark.epic("E2")
@pytest.mark.ticket("T-020")
def test_robots_disallow_is_honored_and_the_crawler_is_identified():
    """C6: robots.txt is honored and the crawler identifies itself rather than a browser."""
    from services.ingest.src.adapters import USER_AGENT, may_fetch

    ua = USER_AGENT
    assert isinstance(ua, str) and len(ua.strip()) >= 3, f"empty user agent: {ua!r}"
    lowered = ua.lower()
    for impersonation in ("mozilla", "applewebkit", "chrome", "safari", "gecko", "edg/"):
        assert impersonation not in lowered, (
            f"C6 requires an identified bot, not browser impersonation: {ua!r}"
        )
    assert "bot" in lowered or "http" in lowered, (
        f"user agent must identify the network (a bot token and/or a contact URL): {ua!r}"
    )

    token = re.split(r"[/\s]", ua.strip())[0]
    assert token, f"user agent has no leading product token: {ua!r}"

    wildcard = "User-agent: *\nDisallow: /admin/\nDisallow: /cart\nAllow: /\n"
    assert may_fetch(wildcard, "https://store.example.com/products.json", ua) is True
    assert may_fetch(wildcard, "https://store.example.com/admin/orders", ua) is False
    assert may_fetch(wildcard, "https://store.example.com/cart", ua) is False

    # An empty robots.txt permits everything.
    assert may_fetch("", "https://store.example.com/policies/shipping", ua) is True

    # A rule naming *our* product token beats the permissive wildcard group.
    targeted = f"User-agent: {token}\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    assert may_fetch(targeted, "https://store.example.com/products.json", ua) is False


# ---------------------------------------------------------------------------------
# T-021 — differential extraction, provenance, quarantine
# ---------------------------------------------------------------------------------


@pytest.mark.epic("E2")
@pytest.mark.ticket("T-021")
def test_unchanged_content_hash_produces_zero_reextraction():
    """C6: unchanged content re-extracts nothing; changed content does."""
    from services.ingest.src.extraction import content_hash, needs_extraction

    page = POLICY_PAGE_TEXT
    changed = POLICY_PAGE_TEXT + "We now also ship to Canada.\n"

    with pytest.MonkeyPatch.context() as mp:
        # The change decision is a local hash comparison: it fetches nothing and calls
        # no model. Anything that reaches for a socket here fails loudly.
        _forbid_network(mp)

        digest = content_hash(page)
        assert isinstance(digest, str) and digest.strip(), f"empty content hash: {digest!r}"
        assert content_hash(page) == digest, "content_hash must be deterministic"
        assert content_hash(changed) != digest, "different content must hash differently"

        assert needs_extraction(digest, page) is False
        assert needs_extraction(digest, changed) is True
        # Never seen before: there is nothing to compare against, so it must extract.
        assert needs_extraction(None, page) is True
        assert needs_extraction(content_hash(changed), changed) is False


@pytest.mark.epic("E2")
@pytest.mark.ticket("T-021")
def test_every_extracted_claim_carries_a_supported_by_source_with_a_snapshot_ref():
    """R8/C6: every extracted claim is SUPPORTED_BY a scraped Source with a snapshot ref."""
    from services.ingest.src.extraction import extract_claims

    result = extract_claims(POLICY_PAGE_TEXT, _scraped_source())
    claims = _upserted(result)
    assert claims, "the policy page must yield at least one atomic claim"

    for claim in claims:
        provenance = _field(claim, "provenance")
        assert _plain(_field(provenance, "source")) == "scraped", (
            f"scraped claim must carry provenance.source == 'scraped': {claim!r}"
        )
        ref = _field(provenance, "ref")
        assert isinstance(ref, str) and ref.strip(), (
            f"claim provenance must carry a non-empty snapshot ref: {claim!r}"
        )
        observed_at = _field(provenance, "observed_at")
        assert observed_at is not None and str(observed_at).strip(), (
            f"claim provenance must carry observed_at: {claim!r}"
        )
        key = _field(claim, "key")
        assert isinstance(key, str) and key.strip(), f"claim must be keyed: {claim!r}"


@pytest.mark.epic("E2")
@pytest.mark.ticket("T-021")
def test_low_confidence_extraction_is_quarantined_not_upserted():
    """C10: extraction below the confidence floor is quarantined, never upserted."""
    from services.ingest.src.extraction import extract_claims

    source = _scraped_source()

    baseline = extract_claims(POLICY_PAGE_TEXT, source, confidence_floor=0.0)
    all_claims = _upserted(baseline)
    assert all_claims, "the policy page must yield at least one claim"
    assert _quarantined(baseline) == [], "nothing is below a floor of 0.0"

    confidences = [_confidence(claim) for claim in all_claims]
    for value in confidences:
        assert 0.0 <= value <= 1.0, f"confidence out of range: {value!r}"

    # Above every observed confidence, the whole batch must be held back — an extractor
    # that upserts regardless of the floor fails here.
    above_all = max(confidences) + 0.01
    strict = extract_claims(POLICY_PAGE_TEXT, source, confidence_floor=above_all)
    assert _upserted(strict) == [], "claims below the floor must not reach the upsert batch"
    assert len(_quarantined(strict)) == len(all_claims), (
        "claims below the floor must land in quarantine, not vanish"
    )

    # At an intermediate floor the two collections partition the batch exactly.
    mid = sorted(confidences)[len(confidences) // 2]
    split = extract_claims(POLICY_PAGE_TEXT, source, confidence_floor=mid)
    kept, held = _upserted(split), _quarantined(split)
    assert len(kept) + len(held) == len(all_claims), "extraction must lose no claim"
    assert all(_confidence(c) >= mid for c in kept)
    assert all(_confidence(c) < mid for c in held)


# ---------------------------------------------------------------------------------
# T-022 — entity resolution
# ---------------------------------------------------------------------------------


@pytest.mark.epic("E2")
@pytest.mark.ticket("T-022")
def test_gtin_matches_always_link_and_below_threshold_pairs_do_not():
    """DESIGN §Data models: GTIN equality always links; below-threshold pairs get no SAME_AS."""
    from services.ingest.src.er import match

    gtin = "00012345678905"
    left = {
        "product_id": "p-1",
        "gtin": gtin,
        "canonical_name": "Trail Runner Shoe",
        "brand": "Cascade",
    }
    # Same GTIN, deliberately unrecognisable name: identity must beat text similarity.
    right = {
        "product_id": "p-2",
        "gtin": gtin,
        "canonical_name": "Zapatilla de trail para hombre",
        "brand": "Cascade",
    }

    linked = match(left, right, 0.95)
    assert _field(linked, "linked") is True, "equal GTINs must always link"
    assert float(_field(linked, "confidence")) == 1.0, "a GTIN match is a certainty"
    assert match(right, left, 0.95) == linked or (
        _field(match(right, left, 0.95), "linked") is True
    ), "entity resolution must be symmetric"

    unrelated_a = {
        "product_id": "p-3",
        "gtin": "00099999999999",
        "canonical_name": "Ceramic Burr Coffee Grinder",
        "brand": "Kettleworks",
    }
    unrelated_b = {
        "product_id": "p-4",
        "gtin": "00088888888888",
        "canonical_name": "Merino Wool Hiking Sock",
        "brand": "Cascade",
    }

    weak = match(unrelated_a, unrelated_b, 0.9)
    assert _field(weak, "linked") is False, "an unrelated pair must not produce SAME_AS"
    weak_confidence = float(_field(weak, "confidence"))
    assert 0.0 <= weak_confidence <= 1.0, "every edge decision carries a confidence in [0,1]"
    assert weak_confidence < 0.9, (
        "a pair refused at threshold 0.9 must score below 0.9 — the confidence and the "
        "link decision have to agree"
    )
    assert float(_field(match(unrelated_a, unrelated_b, 0.9), "confidence")) == (
        weak_confidence
    ), "entity resolution must be deterministic"


# ---------------------------------------------------------------------------------
# T-023 — one CatalogAdapter interface, two implementations
# ---------------------------------------------------------------------------------


@pytest.mark.epic("E2")
@pytest.mark.ticket("T-023")
def test_both_catalog_adapters_satisfy_one_interface():
    """C6: catalog ingestion sits behind one adapter interface both adapters implement.

    The interface has to be the real read path, not a token method: T-020 requires an
    adapter that *ingests a storefront* and T-023 requires the MCP adapter's *mapping to
    graph upserts* to match `signed_fetch` semantics. Both operations are therefore
    named on the protocol, and both adapters must implement both of them with a body of
    their own — an interface neither adapter has to do any work behind is not a seam.
    """
    import inspect

    from services.ingest.src.adapters import CatalogAdapter, catalog_mcp, signed_fetch

    protocol_methods = _public_methods(CatalogAdapter)
    assert protocol_methods, "CatalogAdapter must declare at least one public method"

    missing_from_protocol = sorted(set(_CATALOG_ADAPTER_METHODS) - set(protocol_methods))
    assert not missing_from_protocol, (
        "CatalogAdapter must declare the catalog read path both adapters share; "
        f"missing {missing_from_protocol} (it declares {sorted(protocol_methods)})"
    )
    for name in _CATALOG_ADAPTER_METHODS:
        params = [
            p
            for p in inspect.signature(protocol_methods[name]).parameters
            if p != "self"
        ]
        assert params, (
            f"CatalogAdapter.{name}() takes no arguments besides self; it cannot read a "
            "storefront or map anything"
        )

    implementations = {
        "signed_fetch": _adapter_class(signed_fetch, "signed_fetch"),
        "catalog_mcp": _adapter_class(catalog_mcp, "catalog_mcp"),
    }
    assert implementations["signed_fetch"] is not implementations["catalog_mcp"], (
        "signed_fetch and catalog_mcp must be two distinct adapters"
    )

    for label, cls in implementations.items():
        assert cls is not CatalogAdapter, f"{label} must be an implementation, not the protocol"
        methods = _public_methods(cls)
        missing = sorted(set(protocol_methods) - set(methods))
        assert not missing, f"{label} does not implement CatalogAdapter methods: {missing}"
        for name in _CATALOG_ADAPTER_METHODS:
            assert name in methods, (
                f"{label} does not implement CatalogAdapter.{name}(); both adapters have "
                "to satisfy the same read path"
            )
            assert methods[name] is not protocol_methods[name], (
                f"{label}.{name}() is the inherited protocol declaration, not an "
                f"implementation — {label} does no work behind CatalogAdapter.{name}()"
            )
        for name in sorted(protocol_methods):
            expected = [
                p
                for p in inspect.signature(protocol_methods[name]).parameters
                if p != "self"
            ]
            actual = [
                p for p in inspect.signature(methods[name]).parameters if p != "self"
            ]
            assert actual == expected, (
                f"{label}.{name} signature {actual} does not match "
                f"CatalogAdapter.{name} {expected}"
            )
