"""Reproductions for the exchange tickets that carry a PLACEHOLDER gate.

A ticket cannot be dispatched until a gate is proven red on today's code, and 105 tickets in
this project's graph still read ``false  # NO GATE YET``. This file is the exchange slice of
that backlog: one ``xfail(strict=True)`` test per defect that was *measured* live at HEAD.

The mechanism, in both directions:

* a normal run reports ``xfailed`` and exits 0, so ``pytest apps/exchange -q`` stays green;
* the ticket's gate runs ``--runxfail -k <name>`` and gets a real ``1 failed``;
* ``strict=True`` turns the eventual repair into an XPASS *failure*, so whoever fixes the
  defect must delete the marker. The gate cleans itself up.

Every test below asserts the behaviour that SHOULD hold, never the behaviour that does. A test
that pinned today's output would certify the defect — which is itself a ticket in this set
(T-222), and the reason ``test_accept.py``'s off-domain test was inverted rather than deleted.

Covered here: T-158, T-169 (two halves, separately fixable), T-170, T-204, T-235, and T-250 —
the last scoped to ``packages/contracts`` rather than to this app, filed here because the
exchange is the consumer that measured it and this is the file the lane owns.

Tickets measured and found ALREADY FIXED or not reproducible as described are deliberately
absent: T-145, T-146, T-147, T-148, T-150, T-157, T-159, T-168, T-176, T-182, T-202, T-215 and
T-222. Writing an xfail for a defect that is not there would mint a gate that can never go red
for the reason it names. The lane report carries the measurement for each.

Nothing in this file touches product source. A lane that repairs the defect it was asked to
reproduce destroys the gate that would have graded the repair.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

#: ``apps/exchange/src`` — the production tree, reached from this file rather than from a
#: hard-coded string so a moved test file cannot silently start scanning nothing.
EXCHANGE_SRC = Path(__file__).resolve().parents[1] / "src"

#: The published exchange contract. It is the *specification* of the served surface, which is
#: what makes a path it declares and the app does not serve a defect rather than a preference.
EXCHANGE_OPENAPI = (
    Path(__file__).resolve().parents[3] / "packages" / "contracts" / "openapi" / "exchange.openapi.json"
)


def _calls_named(tree: ast.AST, name: str) -> list[int]:
    """Line numbers of every real *call* to ``name`` in ``tree``.

    AST, not grep, and the difference is the whole point: ``accept/__init__.py``'s wiring
    docstring contains the literal text ``use_registered_domains(StaticRegisteredDomains(...))``
    as an *example*, and a grep counts it as a call site. It is a string constant; the parser
    never sees a ``Call`` node for it, so this cannot be fooled by documentation.
    """
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            found.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            found.append(node.lineno)
    return found


# =====================================================================================
# T-169 — the registered-domain guard is decorative in the deployed configuration
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-169: nothing in production source calls use_registered_domains, so the deployed "
        "exchange resolves no platform registry and registered_domain_for falls back to "
        "bid['store_domain'] — a bidder-controlled field supplying both halves of its own "
        "host check; remove this marker with the fix"
    ),
)
def test_t169_a_production_call_site_wires_the_platform_seller_registry() -> None:
    """The trusted half of the domain check has no deployment that supplies it.

    ``use_registered_domains`` is the seam a deployment is supposed to use, and
    ``test_accept.py::test_the_wired_default_is_used_when_the_caller_passes_nothing`` proves the
    seam works. Nothing turns it. Measured at HEAD::

        $ git grep -n use_registered_domains -- '*.py' | grep -v /tests/
        apps/exchange/src/accept/__init__.py:17:    from apps.exchange.src.accept import ...
        apps/exchange/src/accept/__init__.py:21:    use_registered_domains(StaticRegisteredDomains({...}))
        apps/exchange/src/accept/offer.py:88:    "use_registered_domains",
        apps/exchange/src/accept/offer.py:111:def use_registered_domains(source: Any | None) ...
        apps/exchange/src/accept/offer.py:420:            ... wired by :func:`use_registered_domains` ...

    Every one of those is the definition, its ``__all__`` entry, or a docstring — the two at
    ``__init__.py`` are the wiring *example* inside a module docstring. The AST scan below is
    what tells those apart from a call.

    The consequence is not cosmetic: with no source wired, ``platform_registered_domains()`` is
    ``None``, ``CheckoutRequest(registered_domains=None)`` is built, and the port compares the
    permalink host against ``bid['store_domain']``. A store that writes ``attacker.tld`` into
    both fields agrees with itself and is handed a real single-use discount code — which is why
    T-176 refused to downgrade this ticket: the frozen S8-3 release blocker
    ``test_offdomain_checkout_url_is_refused`` PASSES on that decorative comparison, so
    ``spec_criteria_passing`` counts a security property as met that a consistent liar defeats.

    A fix is one call, anywhere a deployment builds the app. This asserts only that one exists.
    """
    callers: list[str] = []
    for path in sorted(EXCHANGE_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno in _calls_named(tree, "use_registered_domains"):
            # The definition's own recursive/self reference would be inside offer.py; there is
            # none today, but exclude nothing — a call in offer.py that wires a default IS a fix.
            callers.append(f"{path.relative_to(EXCHANGE_SRC)}:{lineno}")

    assert callers, (
        "no production source in apps/exchange/src calls use_registered_domains, so every "
        "deployed accept runs with registered_domains=None and compares the checkout host "
        "against the bidder's own store_domain"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-169: `apps.exchange.src.accept` and `exchange.accept` are the same file (equal "
        "inode) but two distinct module objects with two copies of every module global, so a "
        "registry wired through the spelling the package's own docstring documents is "
        "invisible to the spelling the served app imports; remove this marker with the fix"
    ),
)
def test_t169_a_registry_wired_through_either_spelling_binds_the_other() -> None:
    """Wiring a process-wide default only works if there is one process-wide module.

    ``accept/__init__.py``'s wiring docstring tells an integrator to
    ``from apps.exchange.src.accept import accept, use_registered_domains``; ``main.py`` builds
    the served app under ``exchange.*``; and the frozen acceptance suite
    (``.swarm-loop/acceptance/test_spec_criteria.py:1081``) imports the ``apps.*`` spelling. Both
    resolve to one file — ``apps/`` and ``apps/exchange/`` carry no ``__init__.py``, so they are
    PEP-420 namespace packages off the repo root, while ``.pkgroot/exchange`` is a symlink to the
    same directory. Measured at HEAD::

        os.path.realpath equal : True
        st_ino equal           : True   (239729111 239729111)
        (A is B)               : False
        (A.accept is B.accept) : False
        after A.use_registered_domains(...):
          A.platform_registered_domains() -> <StaticRegisteredDomains ...>
          B.platform_registered_domains() -> None

    ``checkout.registry._REGISTRY`` is two dicts as well, so a provider registered through one
    spelling is invisible to the other. The assertion below is the *state-crossing* one rather
    than plain ``A is B``, because module identity can be satisfied cosmetically while the
    globals stay forked — and it is the forked globals that make the wiring silently do nothing.

    Three in-repo conventions already solve this, and the closest precedent is the third:
    ``packages/store-agent/src/hooks/__init__.py:96`` (``_install_canonical_alias``),
    ``apps/trust/src/ledger/__init__.py:119`` (an elected primary, built for a thread race the
    alias approach did not survive), and ``apps/buyer/svc/src/accept/_spellings.py`` — the same
    package name, the same defect, shipped with ``test_accept_spellings.py``. The exchange has
    no equivalent. (The ticket's claim that ``apps/trust/src/ledger`` implements
    ``_install_canonical_alias`` is wrong; it does not. That does not change the defect.)

    Both spellings are asserted to resolve inside THIS worktree before anything is claimed: the
    venv's ``_proxyshop.pth`` puts a hard-coded checkout on ``sys.path`` for every process using
    it and has already produced one false green in this repo.
    """
    import exchange.accept as served  # noqa: PLC0415
    from exchange.checkout import StaticRegisteredDomains  # noqa: PLC0415

    import apps.exchange.src.accept as documented  # noqa: PLC0415

    here = Path(__file__).resolve().parents[3]
    for label, module in (("apps.exchange.src.accept", documented), ("exchange.accept", served)):
        resolved = Path(str(module.__file__)).resolve()
        assert here in resolved.parents, (
            f"{label} resolved to {resolved}, which is outside the tree under test ({here}) — "
            "that is the _proxyshop.pth leak, not a measurement of this branch"
        )

    platform = StaticRegisteredDomains({"store-a": "store-a.example.com"})
    previous = documented.use_registered_domains(platform)
    try:
        assert served.platform_registered_domains() is not None, (
            "a platform registry wired through `apps.exchange.src.accept` — the spelling this "
            "package's own docstring documents and the frozen acceptance suite imports — is "
            "invisible to `exchange.accept`, the spelling the served app runs on: the same "
            "file is two modules with two copies of every global"
        )
    finally:
        documented.use_registered_domains(previous)


# =====================================================================================
# T-170 — the served app exposes no HTTP path by which an accept can reach accept()
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-170: main.py mounts routers by globbing <feature>/routes.py and the accept package "
        "ships none, so the served exchange exposes only ['/auctions', "
        "'/auctions/{auction_id}'] while the published contract declares "
        "'/auctions/{auction_id}/accept'; remove this marker with the fix"
    ),
)
def test_t170_the_served_exchange_app_exposes_the_published_accept_path() -> None:
    """The published contract declares an accept endpoint; the app that boots does not serve it.

    This is the assertion that makes T-170 a defect rather than a scheduling note. T-176
    downgraded it on the ground that an unwired half is ordinary unfinished work — true of a
    zero-caller *function*, but ``packages/contracts/openapi/exchange.openapi.json`` is a
    published contract, and a path it declares that the service does not answer is a promise
    the platform is already making to clients. Measured at HEAD::

        served    ['/auctions', '/auctions/{auction_id}']
        published ['/auctions', '/auctions/{auction_id}/accept',
                   '/auctions/{auction_id}/shortlist', '/internal/outcomes',
                   '/v1/auctions/{auction_id}/bids']

    Only ``/auctions/{auction_id}/accept`` is asserted here. The other three absences belong to
    scheduled feature tickets that own them (T-032 shortlist, T-044 external bids); this one is
    the path T-176 records as owned by NO ticket, which is exactly why it needs a gate to keep
    it from being lost.

    The whole accept half — the registered-domain check, the mint, the C11 event sequence, the
    orphan record — is complete, tested and lint-enforced, and no deployed request can reach any
    of it.
    """
    from exchange.main import create_app  # noqa: PLC0415

    served = set(create_app().openapi()["paths"])
    published = set(json.loads(EXCHANGE_OPENAPI.read_text(encoding="utf-8"))["paths"])
    accept_path = "/auctions/{auction_id}/accept"

    assert accept_path in published, (
        f"this test's premise moved: {EXCHANGE_OPENAPI} no longer declares {accept_path}"
    )
    assert accept_path in served, (
        f"the published contract declares {accept_path} and the served app answers only "
        f"{sorted(served)} — there is no HTTP path by which a buyer's accept reaches accept()"
    )


# =====================================================================================
# T-204 — `denial_reason` is a persisted, client-visible field with no vocabulary
# =====================================================================================

#: A bid whose offer is on the seller's own domain, so nothing refuses it for an unrelated
#: reason. Rebuilt per test: ``accept()`` MUTATES the auction it is handed (``accepted_bid_ref``).
def _auction() -> dict[str, Any]:
    return {
        "auction_id": "auction-1",
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "bids": [
            {
                "bid_id": "bid-a",
                "store_id": "store-a",
                "store_domain": "store-a.example.com",
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 100.0,
                    "total_price": 100.0,
                    "checkout_url": "https://store-a.example.com/cart/1:1",
                    "expires_at": 2_000_000_000.0,
                },
            },
            {
                "bid_id": "bid-b",
                "store_id": "store-b",
                "store_domain": "store-b.example.com",
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 110.0,
                    "total_price": 110.0,
                    "checkout_url": "https://store-b.example.com/cart/1:1",
                    "expires_at": 2_000_000_000.0,
                },
            },
        ],
        "accepted_bid_ref": None,
        "now": 1_700_000_000.0,
    }


def _declared_denial_vocabulary() -> tuple[str, set[str]] | None:
    """The enumerated ``denial_reason`` vocabulary, from wherever a fix chose to declare it.

    Deliberately not prescriptive about the fix's shape: either the published contract grows an
    ``enum`` beside ``"type": "string"``, or the accept package exports a constant. Both are
    real answers to "this field's vocabulary is an exception class name"; the test only insists
    that *one* of them exists, and that what ``accept()`` emits actually comes from it.
    """
    document = json.loads(EXCHANGE_OPENAPI.read_text(encoding="utf-8"))

    def walk(node: Any) -> set[str] | None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict) and isinstance(properties.get("denial_reason"), dict):
                enum = properties["denial_reason"].get("enum")
                if enum:
                    return {str(value) for value in enum}
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found:
                    return found
        return None

    published = walk(document)
    if published:
        return ("packages/contracts/openapi/exchange.openapi.json", published)

    import exchange.accept as accept_package  # noqa: PLC0415

    for name in ("DENIAL_REASONS", "DENIAL_REASON_VOCABULARY", "REFUSAL_REASONS", "ACCEPT_DENIALS"):
        declared = getattr(accept_package, name, None)
        if declared:
            return (f"exchange.accept.{name}", {str(value) for value in declared})
    return None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-204: denial_reason is persisted into a policy_event payload and published as a bare "
        "string, but its vocabulary is whatever `type(exc).__name__` happens to be — six "
        "distinct leading tokens today, one of which renders a memory address; no enum and no "
        "exported constant declares the set; remove this marker with the fix"
    ),
)
def test_t204_a_denial_reason_is_drawn_from_a_declared_vocabulary() -> None:
    """A client-visible, persisted field whose values are exception class names is a contract
    by accident.

    ``accept()`` builds its refusal as ``f"checkout_refused: {type(exc).__name__}: {exc}"``
    (``apps/exchange/src/accept/offer.py:525``), writes it into the ``policy_event`` payload's
    ``reason``, and returns it on ``AcceptResult.denial_reason``. The published contract types
    it ``{"type": "string"}`` with the example ``"blacklist"`` — a value the code has never
    produced; the eligibility gate emits ``"blacklisted: fixture:blacklisted"``.

    Measured leading tokens at HEAD, by driving each branch::

        ['already_accepted', 'blacklisted', 'checkout_refused', 'unavailable',
         'unknown_bid', 'unrecordable_acceptance']

    and behind the ``checkout_refused:`` prefix, nine exception classes so far — including
    ``TypeError``, which renders ``registered_domains <object object at 0x…> exposes neither
    domain_for(store_id) nor __call__(store_id)`` and puts a memory address into a persisted
    event. Renaming any of those classes silently changes a published value, and nothing in the
    repo would notice: the two assertions that touch ``denial_reason``
    (``test_orphaned_code.py:1024`` and ``:1623``) pin two individual strings, not the set.

    This asserts the minimum that makes it a contract on purpose: a declared vocabulary exists,
    and what ``accept()`` actually emits is drawn from it. Either declaration site counts.
    """
    from exchange.accept import accept  # noqa: PLC0415

    class Exploding:
        def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
            raise RuntimeError(f"merchant POST /codes is down for {store_id}")

        __call__ = create_code

    class Silent:
        def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
            return {"code": "PSX-AAAA1111", "permalink_url": "https://store-a.example.com/c/1"}

        __call__ = create_code

    accepted_once = _auction()
    accepted_once["accepted_bid_ref"] = "bid-a"

    emitted = [
        accept(_auction(), "bid-nowhere", Silent(), "shopify").denial_reason,
        accept(accepted_once, "bid-b", Silent(), "shopify").denial_reason,
        accept(_auction(), "bid-a", Exploding(), "shopify").denial_reason,
    ]
    tokens = sorted({str(reason).split(":", 1)[0] for reason in emitted})

    declared = _declared_denial_vocabulary()
    assert declared is not None, (
        "denial_reason is persisted and published, and nothing declares its vocabulary: the "
        "contract types it a bare string and no constant enumerates it. Measured tokens "
        f"accept() emits today: {tokens}"
    )
    where, vocabulary = declared
    unknown = [token for token in tokens if token not in vocabulary]
    assert not unknown, (
        f"accept() emits denial_reason values {unknown} that {where} does not declare; the "
        f"declared vocabulary is {sorted(vocabulary)}"
    )


# =====================================================================================
# T-158 — the one-accept-per-auction guard is scoped to a Python object, not the auction
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-158: accept() stamps `accepted_bid_ref` on the auction OBJECT it is handed and "
        "persists nothing, so two requests that each load the same auction record and never "
        "save it back both pass the one-accept guard and mint two live single-use discount "
        "codes for one purchase; remove this marker with the fix"
    ),
)
def test_t158_a_second_accept_on_a_reloaded_auction_record_mints_no_second_code() -> None:
    """One auction must yield one discount code, whichever Python object carries it.

    ``accept()`` takes an auction, a bid id, a code creator and a mode — no store, no state
    machine — and closes the auction by writing ``accepted_bid_ref`` onto the object in front of
    it (``apps/exchange/src/accept/offer.py:259-263``, called at ``:532``). The guard that reads
    it back (``offer.py:449-458``) therefore holds for exactly as long as that one object does.
    ``offer.py:41-48`` states the limit in prose; this is that paragraph as a gate.

    Measured at HEAD, one auction id saved once and loaded twice::

        first  accepted: True  code: PSX-YPTECE8K
        second accepted: True  code: PSX-CWGTFNA7
        codes differ   : True

    Two complete C11 event trios, two live single-use discounts, one purchase — a discount the
    seller never agreed to. The control below (the same object twice) proves the guard exists
    and is object-scoped, so a green here cannot come from the auction being unacceptable.

    ``AuctionRecord`` round-trips through ``InMemoryAuctionStore`` exactly as ``RedisAuctionStore``
    does (``state.py:136-139`` deserialises from JSON on every ``load``), so two loads are two
    unstamped records for free — which is precisely the route shape the ticket describes.

    The fix has a direct in-repo precedent: ``apps/buyer/svc/src/accept/handoff.py:187`` keeps a
    process-wide ``AcceptLedger`` that ``accept`` consults when none is injected, so "one auction
    gets one checkout" survives the object. This test calls ``accept()`` in its deployed
    four-positional shape on purpose: a seam that must be passed explicitly to be safe leaves
    every real caller unguarded.
    """
    from exchange.accept import accept  # noqa: PLC0415
    from exchange.auction.state import CLOSED, AuctionRecord, InMemoryAuctionStore  # noqa: PLC0415
    from exchange.checkout import StaticRegisteredDomains  # noqa: PLC0415

    store = InMemoryAuctionStore()
    store.save(
        AuctionRecord(
            auction_id="auction-dup",
            intent_id="intent-1",
            cluster_id="cluster-1",
            state=CLOSED,
        )
    )
    platform = StaticRegisteredDomains({"store-a": "store-a.example.com"})

    def one_request() -> dict[str, Any]:
        """What a route does: load the record, hand accept() a view of it, never save back."""
        record = store.load("auction-dup")
        assert record is not None
        view = _auction()
        view["auction_id"] = record.auction_id
        view["accepted_bid_ref"] = record.accepted_bid_ref
        return view

    first = accept(one_request(), "bid-a", None, "redirect", registered_domains=platform)
    second = accept(one_request(), "bid-a", None, "redirect", registered_domains=platform)

    # Control, and it must stay true after the fix: the first accept succeeds and mints.
    assert first.accepted is True, f"the first accept was refused: {first.denial_reason}"
    assert first.code is not None

    assert second.accepted is False, (
        "a second request that loaded the same auction record minted a second live single-use "
        f"discount code ({first.code!r} and {second.code!r}) for one purchase — the one-accept "
        "guard is scoped to the Python object accept() was handed, not to the auction"
    )
    assert second.code is None, second.code


# =====================================================================================
# T-235 — one ledger kind, two bodies: the success path emits none of its published keys
# =====================================================================================


def test_t235_a_successful_checkout_emits_the_published_code_created_body() -> None:
    """One ledger kind must have one body, and the contract says which.

    ``packages/contracts/src/ledger.py:70`` publishes
    ``LEDGER_PAYLOAD_SHAPES['code_created'] = ('code', 'permalink_url', 'expires_at')``. The
    success path (``apps/exchange/src/checkout/provider.py:982-986``) writes
    ``{'checkout_token': ..., 'discount_code': ...}`` — none of the three. The ORPHAN path
    (``apps/exchange/src/accept/offer.py:294-311``) writes the published body and its own comment
    says so. Measured at HEAD::

        ['checkout_token', 'discount_code']
        ["'code_created' payload is missing published key 'code'",
         "'code_created' payload is missing published key 'permalink_url'",
         "'code_created' payload is missing published key 'expires_at'"]

    while its two siblings on the same emit are clean: ``accepted`` -> ``[]`` and
    ``checkout_redirect`` -> ``[]``. So a consumer reading ``payload['code']`` off a *successful*
    ``code_created`` gets nothing — including anything that needs to revoke or expire a live
    discount, which is exactly what T-202's orphan machinery was built to enable.

    The cause is that nothing on this path calls ``validate_ledger_payload``, though
    ``contracts/src/ledger.py:15`` says the check belongs at the PRODUCING boundary. The
    convention already exists in-repo: ``apps/merchant/svc/src/codes/ledger.py:66`` wraps
    ``build_event`` so a drifting body raises ``MalformedLedgerPayload``; the exchange's
    ``auction/ledger.py:71`` checks only that the kind is in the frozen enum.

    ``services/sim/tests/test_simulation.py::test_no_kind_other_than_code_created_deviates_from_its_published_payload``
    must not be mistaken for a guard on this: it asserts ``deviating <= {"code_created"}``, i.e.
    it whitelists this exact defect by name, and stays green both before and after the repair.

    The cross-field assertions matter as much as the key check — the data is not missing, it is
    on ``AcceptResult`` already, so an event carrying the keys with the wrong values would be a
    worse outcome than one carrying none.
    """
    from contracts.ledger import LEDGER_PAYLOAD_SHAPES, validate_ledger_payload  # noqa: PLC0415
    from exchange.accept import accept  # noqa: PLC0415
    from exchange.checkout import StaticRegisteredDomains  # noqa: PLC0415

    result = accept(
        _auction(),
        "bid-a",
        None,
        "redirect",
        registered_domains=StaticRegisteredDomains({"store-a": "store-a.example.com"}),
    )
    assert result.accepted is True, f"premise moved: the accept was refused ({result.denial_reason})"

    created = [event for event in result.events if event["kind"] == "code_created"]
    assert len(created) == 1, [event["kind"] for event in result.events]
    payload = dict(created[0]["payload"])

    problems = validate_ledger_payload("code_created", payload)
    assert problems == [], (
        f"the successful checkout's code_created body is {sorted(payload)}, and the published "
        f"shape is {list(LEDGER_PAYLOAD_SHAPES['code_created'])}: {problems}"
    )
    assert payload["code"] == result.code, (payload.get("code"), result.code)
    assert payload["permalink_url"] == result.permalink_url
    assert payload["expires_at"] == result.expires_at


# =====================================================================================
# T-250 — the shared boundary's price floor is an exact equality, so 0.001 clears it
# =====================================================================================

#: One product, priced by the exchange's own roster. `list_prices` is the authoritative catalog
#: the auction was opened from — the boundary reads it and a carried claim cannot silence it.
T250_ROSTER: dict[str, float] = {"prod-1": 100.0}

#: A hook-shaped provenance block, so a declared depth is legibly authorized paperwork rather
#: than something the walk refuses for an unrelated reason.
T250_PROVENANCE: dict[str, Any] = {
    "source": "envelope_rule",
    "ref": "envelope:store-1:v3#max_discount_pct@prod-1",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 1,
}


def _t250_bid(price: float, depth: float | None = None) -> dict[str, Any]:
    offer: dict[str, Any] = {
        "product_ref": "prod-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "expires_at": "2999-01-01T00:00:00Z",
    }
    if depth is not None:
        offer["discount"] = {
            "type": "percentage",
            "value": depth,
            "provenance": dict(T250_PROVENANCE),
        }
    return {"auction_id": "auc-1", "store_id": "store-1", "offer": offer, "claims": []}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-250: packages/contracts/src/boundary.py's roster price floor tests `priced == 0.0`, "
        "an EXACT equality, so price_reasons() returns [] — no objection — for a unit_price of "
        "0.001 on a product its own roster prices at 100.00; a sibling lane closed this at the "
        "exchange door only, and the shared boundary every other consumer runs on, including "
        "the TypeScript peer, still carries the equality; remove this marker with the fix"
    ),
)
def test_t250_the_shared_boundary_refuses_a_thousandth_of_a_cent_for_a_hundred_dollar_product() -> (
    None
):
    """The floor is the one relation in the price walk no declared depth can satisfy — and it
    only holds at exactly zero.

    Every other check in ``_price_reasons`` is an inequality against the authorized depth, so an
    authorized 100 makes all of them true at once: ``listed * (100 - 100) / 100`` is 0.00, and any
    price at or above that reconciles. The floor is what stops the free item — and it is written
    ``priced == 0.0``, so the attacker's answer is to write ``0.001`` instead of ``0.0``.

    Measured against ``contracts.boundary`` at HEAD, roster ``{'prod-1': 100.0}``::

        0.001 declared 100, cap 100  -> []
        1e-09 undeclared,   cap 100  -> []
        0.0   declared 100, cap 100  -> ['price_unreconcilable:offer.unit_price:not_positive',
                                         'price_unreconcilable:offer.total_price:not_positive']

    An empty reason list is the boundary saying it has no objection to the price. So the wall
    refuses the absence of a price spelled ``0.0`` and admits the same absence spelled ``0.001``.

    This is the SHARED door, not the exchange's copy: ``price_reasons`` is published precisely so
    a caller holding a catalog but no trust snapshot reaches the same arithmetic (``boundary.py``
    says so at the function's own docstring — "It is the SAME function ``validate_bid`` calls, not
    a re-implementation: a second copy of this arithmetic is a second thing to keep in step with
    the TypeScript door"). A fix applied at ``apps/exchange/src/auction/collect.py`` leaves every
    other consumer, and the TypeScript peer that mirrors this file, on the equality.

    The two controls at the end are not decoration and must stay green through the fix. R10
    compels the exchange to admit an aggressive undercut, so 80.00 and even 1.00 on this roster
    row are LEGAL and a floor that refuses them is a worse defect than the one it replaced. One
    billionth of a cent is not an undercut; it is below the smallest unit the currency has.
    """
    from contracts.boundary import price_reasons  # noqa: PLC0415

    declared = price_reasons(
        _t250_bid(0.001, depth=100.0), list_prices=T250_ROSTER, max_discount_pct=100.0
    )
    assert declared, (
        "the shared boundary raised no objection to charging 0.001 for a product its own roster "
        "prices at 100.00, under a declared and authorized depth of 100%"
    )

    silent = price_reasons(_t250_bid(1e-09), list_prices=T250_ROSTER, max_discount_pct=100.0)
    assert silent, (
        "the shared boundary raised no objection to charging 1e-09 for a 100.00 product with no "
        "declared discount at all"
    )

    # Controls. These are true today and MUST remain true after the fix.
    assert price_reasons(
        _t250_bid(0.0, depth=100.0), list_prices=T250_ROSTER, max_discount_pct=100.0
    ), "the exact-zero floor stopped working; the fix widened the wrong relation"
    for legal in (80.0, 1.0):
        assert (
            price_reasons(_t250_bid(legal), list_prices=T250_ROSTER, max_discount_pct=100.0) == []
        ), f"R10's aggressive undercut at {legal:.2f} was refused; the floor is now too high"
