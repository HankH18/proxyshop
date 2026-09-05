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
    Path(__file__).resolve().parents[3]
    / "packages"
    / "contracts"
    / "openapi"
    / "exchange.openapi.json"
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
    assert result.accepted is True, (
        f"premise moved: the accept was refused ({result.denial_reason})"
    )

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


# =====================================================================================
# Served-vs-published sweep — the shared machinery for T-312 (and the shape T-309 uses
# next door in ``packages/store-agent/tests``)
# =====================================================================================
#
# A hand-written probe over one or two named routes blocks exactly one way of being wrong;
# ``test_t170_…`` above is that shape, deliberately, because it grades a single path T-176
# left unowned. What follows is the general property instead: build the app, read
# ``app.openapi()['paths']``, read the service's published contract, and require the two
# operation sets to agree **in both directions**. It keeps holding as routes are added on
# either side, and it catches the divergence that runs the other way — a route the service
# answers that no contract declares — which a frozen list of names cannot see at all.

#: ``packages/contracts/openapi`` — derived from the constant above so a moved contract
#: directory breaks loudly here rather than silently making every sweep iterate nothing.
CONTRACTS_OPENAPI_DIR = EXCHANGE_OPENAPI.parent

TRUST_OPENAPI = CONTRACTS_OPENAPI_DIR / "trust.openapi.json"

#: A third service, in the table as the sweep's LIVENESS control: it is a real app that
#: really does serve routes, so ``served`` coming back empty for it means the extractor is
#: broken rather than that a service is unwired.
#:
#: It is deliberately NOT described as a service whose surface agrees with its contract — an
#: earlier comment here claimed that and it is false. Measured: ``merchant_svc.main`` serves
#: NINE operations against a contract publishing SIX; ``GET /install``, ``GET
#: /install/callback`` and ``GET /install/shops`` are served and declared nowhere. So T-312's
#: "three services" is an undercount — a fourth service diverges, in the served-but-
#: unpublished direction, and no ticket names it. That is reported rather than gated here:
#: this lane owns no merchant file, and inventing a fourth half of someone else's ticket
#: inside the exchange's repro file is how gates become unownable.
MERCHANT_OPENAPI = CONTRACTS_OPENAPI_DIR / "merchant.openapi.json"

#: The methods an OpenAPI path item may carry. Everything else under a path item
#: (``parameters``, ``summary``, ``$ref``, ``servers``) is not an operation, and counting it
#: as one would inflate the very non-zero check that arms this sweep.
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _normalise_route(path: str) -> str:
    """``/stores/{store_id}/trust`` -> ``/stores/{}/trust``.

    The comparison is about the *wire shape* of a route, not about what a service names its
    path parameter: a service answering ``/stores/{sid}/trust`` genuinely satisfies the
    contract's ``/stores/{store_id}/trust``, and failing it for the spelling would make this
    gate red for a reason the ticket is not about. Every failure message still prints the raw
    spellings, so a real naming divergence stays visible without being fatal.

    Written with ``str.partition`` rather than a regex so no module-level import has to be
    added to the head of this file (E402). Its behaviour on malformed input is stated exactly,
    because an earlier draft of this docstring claimed something else and was wrong: a ``{``
    with no ``}`` anywhere after it is passed through unchanged, but ``/a/{b/{c}`` collapses
    ``b/{c`` into a single ``{}`` — the first ``{`` pairs with the only ``}``. Nothing in the
    five contracts is shaped like that today, and the injectivity check in the arming test is
    what keeps a future one from collapsing two distinct paths onto one string unnoticed.
    """
    out: list[str] = []
    rest = path
    while "{" in rest:
        head, _, tail = rest.partition("{")
        _param, closed, rest = tail.partition("}")
        if not closed:
            return "".join(out) + head + "{" + tail
        out.append(head + "{}")
    return "".join(out) + rest


def _operations(paths: dict[str, Any], *, raw: bool = False) -> set[tuple[str, str]]:
    """``{(METHOD, path)}`` from an OpenAPI ``paths`` object, normalised unless ``raw``."""
    return {
        (method.upper(), path if raw else _normalise_route(path))
        for path, item in paths.items()
        for method in item
        if method.lower() in HTTP_METHODS
    }


def _published_operations(contract: Path, *, raw: bool = False) -> set[tuple[str, str]]:
    """What an OpenAPI document on disk declares."""
    document = json.loads(contract.read_text(encoding="utf-8"))
    return _operations(document.get("paths", {}), raw=raw)


def _served_operations(app: Any) -> set[tuple[str, str]]:
    """What a built FastAPI application actually answers."""
    return _operations(app.openapi().get("paths", {}))


def _operation_divergence(served: set[tuple[str, str]], published: set[tuple[str, str]]) -> str:
    """A message naming BOTH differences, and the counts each side actually iterated."""
    unserved = sorted(f"{method} {path}" for method, path in published - served)
    unpublished = sorted(f"{method} {path}" for method, path in served - published)
    return (
        f"served {len(served)} operation(s), contract publishes {len(published)}; "
        f"published but NOT served: {unserved or 'none'}; "
        f"served but NOT published: {unpublished or 'none'}"
    )


def _contract_probe_app(contract: Path) -> Any:
    """A synthetic app serving exactly what ``contract`` publishes — the sweep's arming device.

    Three sweeps in this repo were found going QUIET rather than red (T-229 6->0 of 8, T-281
    70->0 of 79, T-241 48->0 of 66): a loop that iterates zero cases and passes. A
    served-vs-published comparison has the same hazard in a nastier form, because
    ``set() == set()`` is a *pass*. Pointing the extractor at an app whose served set is known
    — built out of the very paths under test — is what makes an empty ``served`` mean "this
    service serves nothing" rather than "this probe can no longer see routes".
    """
    from fastapi import FastAPI  # noqa: PLC0415 - kept out of this file's frozen import head

    def _probe() -> dict[str, Any]:  # pragma: no cover - mounted, never called
        return {}

    app = FastAPI(title=f"probe:{contract.name}")
    for method, path in sorted(_published_operations(contract, raw=True)):
        app.add_api_route(path, _probe, methods=[method])
    return app


def _build(module_name: str) -> Any:
    """``create_app()`` for one service, by dotted module name."""
    import importlib  # noqa: PLC0415 - kept out of this file's frozen import head

    return importlib.import_module(module_name).create_app()


#: Every service this file sweeps: ``name -> (app module, published contract)``. The sweep
#: iterates THIS, and the arming test below asserts it is non-empty and that every entry
#: yields a non-zero published operation count — so a table that quietly emptied, or a
#: contract that quietly stopped parsing, is a failure rather than a silent green.
SWEPT_SERVICES: dict[str, tuple[str, Path]] = {
    "exchange": ("exchange.main", EXCHANGE_OPENAPI),
    "trust": ("trust.main", TRUST_OPENAPI),
    "merchant": ("merchant_svc.main", MERCHANT_OPENAPI),
}


def test_the_served_versus_published_sweep_is_armed() -> None:
    """Not xfail, and not optional: the T-312 gates below are worthless without this.

    Four ways the comparison could pass while measuring nothing, all closed here:

    * the service table empties, so the sweep iterates zero services;
    * a contract stops parsing to operations, so ``published - served`` is empty;
    * the extractor stops seeing routes for structural reasons (a changed FastAPI, a swallowed
      exception inside ``app.openapi()``), so ``served`` is empty for *every* app and an
      unserved contract is indistinguishable from a served one;
    * the two sides normalise paths differently, which is the one way a set comparison can be
      wrong without either side being empty.

    The last two are closed by comparing each contract against a synthetic app built from that
    contract's own raw paths, and by requiring at least one real service to serve something.
    """
    assert len(SWEPT_SERVICES) >= 3, (
        f"the sweep table holds {len(SWEPT_SERVICES)} service(s); it is supposed to cover at "
        "least exchange, trust and merchant"
    )

    published_counts: dict[str, int] = {}
    for name, (_module, contract) in sorted(SWEPT_SERVICES.items()):
        assert contract.is_file(), f"{name}: {contract} does not exist"
        raw = _published_operations(contract, raw=True)
        published = _published_operations(contract)
        assert published, f"{name}: {contract} declares no operations; the sweep would be blind"
        published_counts[name] = len(published)

        # Normalisation must be INJECTIVE, or the whole comparison silently shrinks. Two
        # distinct published paths that normalise to one string — `/stores/{store_id}` beside
        # `/stores/{slug}` — collapse identically on BOTH sides, so the probe check below
        # still passes while a service serving only one of them satisfies `served ==
        # published` with the other door 404ing. Nothing in the five contracts is shaped like
        # that today; this is what keeps it that way.
        assert len(published) == len(raw), (
            f"{name}: normalising path parameters collapsed {len(raw)} published operations "
            f"onto {len(published)} — two distinct contract paths differ only in the NAME of "
            "a path parameter, so the served-vs-published comparison can no longer tell them "
            f"apart. Raw: {sorted(f'{m} {p}' for m, p in raw)}"
        )

        probe = _served_operations(_contract_probe_app(contract))
        assert probe == published, (
            f"{name}: the extractor and the contract reader disagree on an app built from the "
            f"contract itself — {_operation_divergence(probe, published)}"
        )

    # PER SERVICE, not summed. `sum(...) > 0` was the first spelling here and it is too weak:
    # it stays green while `exchange.main` regresses to zero routes, as long as one of the
    # other two still serves something — and then the exchange T-312 gate below keeps
    # reporting xfail for a reason that is not the ticket.
    served_counts = {
        name: len(_served_operations(_build(module)))
        for name, (module, _contract) in sorted(SWEPT_SERVICES.items())
    }
    blind = {name: n for name, n in served_counts.items() if n == 0}
    assert not blind, (
        f"these swept services served no operation at all: {blind} (all counts: "
        f"{served_counts}) — for any service in that state the gates below cannot tell "
        "'serves nothing' from 'cannot be measured'"
    )
    served_floors = {"exchange": 3, "trust": 6}
    lost = {n: c for n, c in served_counts.items() if c < served_floors.get(n, 0)}
    assert not lost, (
        f"a swept service lost served routes since these gates were measured — {lost} against "
        f"the floors {served_floors}. Deleting routes so the sets agree is not a fix."
    )
    # FLOORS, not equalities, and the asymmetry is the point. A contract that GROWS is
    # ordinary progress and must not turn this file red for a lane that owns neither it nor
    # the gates below. A contract that SHRINKS is the cheapest way to fake a T-312 fix —
    # ``served == published`` is satisfiable from either side, as the positive control for
    # this file's ingest sibling demonstrated by repairing the gate with a contract edit and
    # no source change at all — so deleting a published promise to make the sets agree trips
    # here. The strict xfail markers below catch the same fake once (XPASS is a failure until
    # someone deletes the marker); this is the tripwire that survives the marker's removal.
    # Only the two services this file actually gates. Merchant is in the table as a liveness
    # control, not as a graded surface, and floor-ing its contract here would turn THIS file
    # red for a merchant-lane change that has nothing to do with T-310 or T-312 — a gate whose
    # failures land on a lane that cannot act on them is a gate that gets deleted.
    floors = {"exchange": 5, "trust": 4}
    shrunk = {name: n for name, n in published_counts.items() if n < floors.get(name, 0)}
    assert not shrunk, (
        "a published contract lost operations since these gates were measured — "
        f"{shrunk} against the floors {floors}. Serving a promise is a fix; deleting the "
        "promise so the sets agree is not. Re-read the contract and, if the removal is "
        "deliberate, lower the floor in the same change that justifies it."
    )


# =====================================================================================
# T-312 — published routes that no server answers, and served routes no contract declares
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-312 (exchange half): exchange.main.create_app() serves POST /auctions, "
        "GET /auctions/{auction_id} and POST /auctions/{auction_id}/accept, while the "
        "published contract declares GET /auctions/{auction_id}/shortlist, "
        "POST /internal/outcomes and POST /v1/auctions/{auction_id}/bids — all three measured "
        "404 — and declares nothing at all for the GET /auctions/{auction_id} the app does "
        "serve; remove this marker with the fix"
    ),
)
def test_t312_the_exchange_serves_exactly_the_operations_its_contract_publishes() -> None:
    """Three published doors nobody answers, and one served door nobody published.

    Measured at HEAD by building the app and reading ``app.openapi()['paths']``::

        served     POST /auctions
                   GET  /auctions/{auction_id}
                   POST /auctions/{auction_id}/accept
        published  POST /auctions
                   POST /auctions/{auction_id}/accept
                   GET  /auctions/{auction_id}/shortlist      -> 404
                   POST /internal/outcomes                    -> 404
                   POST /v1/auctions/{auction_id}/bids        -> 404

    The three 404s are the shortlist a buyer is supposed to read, the outcome callback the
    bandit is supposed to learn from, and the external bid submission a store is supposed to
    use — each fully built and tested as a library and reachable by no request. The fourth
    difference runs the other way: ``GET /auctions/{auction_id}`` is a live, unauthenticated
    read of auction state that appears in no contract, so no client can be told it exists and
    no reviewer of the contract can see that it does.

    Both directions are asserted together on purpose. Serving the three missing paths while
    leaving the fourth undeclared leaves the surface still diverging from its specification,
    which is the property this gate is for — not a checklist of three names.
    """
    served = _served_operations(_build("exchange.main"))
    published = _published_operations(EXCHANGE_OPENAPI)

    assert published, "the exchange contract declares nothing; the sweep is unarmed"
    assert served == published, (
        f"the exchange's served surface diverges from its published contract — "
        f"{_operation_divergence(served, published)}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-312 (trust half): trust.main.create_app() serves only the six raw-ledger "
        "/events operations, while the published contract declares GET /snapshot, "
        "GET /stores/{store_id}/trust and POST /feedback/{order_ref} — all three measured "
        "404 — and declares none of the five read-side /events operations the app does "
        "serve; remove this marker with the fix"
    ),
)
def test_t312_the_trust_service_serves_exactly_the_operations_its_contract_publishes() -> None:
    """The trust service exposes its ledger and none of the reads the platform is promised.

    Measured at HEAD::

        served     POST /events            GET /events        GET /events/head
                   GET  /events/replay     GET /events/verify GET /events/{event_id}
        published  POST /events
                   GET  /snapshot                  -> 404
                   GET  /stores/{store_id}/trust   -> 404
                   POST /feedback/{order_ref}      -> 404

    So the only operation the two sides agree on is the append. The trust *scores* — the whole
    point of the service, and R12's input to the exchange's eligibility gate — are readable by
    nobody, and the buyer feedback that is supposed to move them has no door. Inversely, five
    read paths over the raw event log are served with no contract declaring them.

    This half lives in the exchange's repro file rather than under ``apps/trust/tests`` because
    it needs no file there: it imports ``trust.main`` and reads
    ``packages/contracts/openapi/trust.openapi.json``, both of which this test can reach from
    here. Nothing about the property is exchange-specific; only the file ownership is.
    """
    served = _served_operations(_build("trust.main"))
    published = _published_operations(TRUST_OPENAPI)

    assert published, "the trust contract declares nothing; the sweep is unarmed"
    assert served == published, (
        f"the trust service's served surface diverges from its published contract — "
        f"{_operation_divergence(served, published)}"
    )


# =====================================================================================
# T-310 — the published ranking is on no served path
# =====================================================================================


def _exchange_app_import_closure() -> dict[str, str]:
    """Every ``exchange.*`` module that BUILDING the real app pulls in, name -> file.

    Measured in a SUBPROCESS on purpose, the same way ``services/ingest/tests`` measures its
    own reachability question. In-process, ``sys.modules`` already holds whatever the rest of
    this directory's tests imported — ``test_ranking.py`` imports ``exchange.ranking`` by
    hand — and a reachability question answered against a polluted module table answers
    itself in the affirmative every time.

    The subprocess is given an explicit ``PYTHONPATH`` and its answers are checked against the
    repo root by the caller, because this venv's ``site-packages/_proxyshop.pth`` puts a
    checkout root and its ``.pkgroot`` on ``sys.path`` for every process that uses it — a
    probe that trusts the resolution it happens to get can measure a different tree than the
    one under test.
    """
    import os  # noqa: PLC0415 - kept out of this file's frozen import head
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    repo_root = Path(__file__).resolve().parents[3]
    code = (
        "import json, sys\n"
        "import exchange.main\n"
        "exchange.main.create_app()\n"
        "print(json.dumps({name: getattr(module, '__file__', '') or ''\n"
        "                  for name, module in sys.modules.items()\n"
        "                  if name == 'exchange' or name.startswith('exchange.')}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), str(repo_root / ".pkgroot")])
    env["PROXYSHOP_WORKER"] = env.get("PROXYSHOP_WORKER", "0")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"the exchange app would not build:\n{completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_the_exchange_import_closure_probe_is_armed() -> None:
    """Not xfail, and this separation is the whole reason it exists.

    These four checks used to live inside ``test_t310_…``'s body. That was unsound, and
    measurably so: under ``xfail(strict=True)`` **any** exception in the body is reported
    ``xfailed`` — which is green — so a probe that had stopped working was indistinguishable
    from the defect it was supposed to detect. A faked ``subprocess.run`` returning either
    ``{}`` (empty closure) or a non-zero exit reported ``1 xfailed`` in both cases; the
    subprocess failing to build the app, a JSON decode error and a timeout all read the same
    way. Worse, under the ticket's own ``--runxfail`` gate every one of those dead-probe
    states reads as "still broken", so a lane repairing T-310 would churn against a probe
    that could never go green.

    Moving them out is what makes the failure legible: a broken probe now fails ``make
    verify`` in its own name, while T-310 stays a clean statement about one property.

    What is armed here:

    * the probe subprocess builds the app at all;
    * every module it names resolves inside THIS tree, so the venv's ``_proxyshop.pth`` — which
      puts a checkout root on ``sys.path`` for every process using it — cannot answer the
      question with a different checkout's code;
    * the closure contains the modules already known to be served, so an empty ranking result
      means "ranking is unreachable" rather than "the probe saw nothing".
    """
    repo_root = Path(__file__).resolve().parents[3]
    modules = _exchange_app_import_closure()

    assert modules, "building the exchange app imported no exchange module at all; probe is wrong"
    for name, filename in sorted(modules.items()):
        if not filename:
            continue
        resolved = Path(filename).resolve()
        assert repo_root in resolved.parents, (
            f"{name} resolved to {resolved}, which is outside the tree under test "
            f"({repo_root}) — the probe measured the wrong checkout"
        )

    for expected in ("exchange.main", "exchange.auction.routes", "exchange.accept.routes"):
        assert expected in modules, (
            f"{expected} is missing from the app's own import closure {sorted(modules)} — "
            "the reachability probe is not measuring the served app"
        )


def test_t310_the_served_exchange_app_reaches_the_published_ranking() -> None:
    """The ranker must be imported by the process that serves auctions.

    **Everything below "Measured at HEAD" is the defect as it stood when this gate was
    written, kept because a gate that forgets what it was for is a gate somebody deletes.**
    It is no longer the present tense: the wiring landed with the removal of this test's
    ``xfail`` marker, and the two paragraphs at the end say what replaced it.

    ``apps/exchange/src/ranking`` is T-032's deliverable: the published deterministic scoring
    formula, the hard-constraint eligibility filters, and the shortlist construction. It has
    its own suite (``apps/exchange/tests/test_ranking.py``) and the frozen acceptance suite
    imports ``apps.exchange.src.ranking`` at eight sites across two files. Every one of those
    is a *library* call. Measured at HEAD::

        $ git grep -n ranking -- apps/exchange/src | grep -v '^apps/exchange/src/ranking/'
        apps/exchange/src/accept/offer.py:226:      ... through ranking has no shortlist ...
        apps/exchange/src/auction/routes.py:143:    ... wins every ranking there is ...
        apps/exchange/src/eligibility/__init__.py:23: ... owns the ranking gate inside rank() ...
        apps/exchange/src/retrieval/criteria.py:331: ... apps/exchange/src/ranking never sees ...
        apps/exchange/src/retrieval/rerank.py:18:   ... ranking function for the published one ...

    Five hits outside the package, and every one is a *docstring or comment* — not one is an
    import or a call. (The brief that commissioned this gate said the grep "returns zero
    hits", which is false as written; what is true, and is the defect, is that zero of the
    hits are code.) Inside the package the only imports of a ranking module are
    ``ranking/__init__.py:46`` and ``ranking/scoring.py:32``, both
    ``from contracts.ranking import ...`` — the package reaching for its own contracts
    package. The auction route's import block, lines 39-42, is::

        from ..eligibility import StaticSellerEligibility
        from ..orchestration import solicit_bids
        from .fanout import parallel_fan_out
        from .state import AuctionStateMachine, UnknownAuction

    so a request that runs an auction end to end never scores anything: the offers come back
    from the fan-out in whatever order they arrive. ``docs/demo/starting-slice.md`` 3.4 states
    that "``exchange.ranking.rank`` applies the hard-constraint filters, then the published
    weighted formula, and builds the shortlist" — a claim about a code path no served request
    takes, and it is the weighted formula half that the demo script promises. The filters
    inside
    ``rank()`` are the part that matters most: R19's hard constraints never run on a served
    auction, so a candidate ``rank()`` would have excluded is offered to the buyer anyway.

    The property asserted is reachability, not a call site: some module the app imports while
    building must pull ``exchange.ranking`` in. That is deliberately the weakest sufficient
    condition, so any honest wiring passes — a route that calls ``rank``, a service module
    that imports it, an orchestration step that scores the fan-out — while today's tree, in
    which nothing on the served side names the package at all, cannot.

    **The image blocker this docstring used to end on is GONE, and the sentence is corrected
    rather than deleted so nobody re-derives a phantom.** What was true until ``cf07eab``:
    ``exchange.ranking.filters:39`` does ``from ..retrieval.criteria import HardCriterion,
    MalformedIntent``, which executes ``exchange.retrieval.__init__``, which imports
    ``sources``/``service``, which do ``from ingest.graph import ...`` and ``from
    ingest.embeddings import ...`` at module scope — so importing ``exchange.ranking`` pulls
    in eleven ``ingest.*`` modules, and ``apps/exchange/Dockerfile`` shipped no ``ingest``.
    Wiring ranking in would have turned a booting service into a crash loop. **T-299 shipped
    the library**: the Dockerfile now copies ``services/ingest/src/{__init__.py,graph/,
    embeddings/}`` and creates the third ``.pkgroot`` link, and the artifact gate measures the
    exchange at 0 broken modules of 79 by importing inside a real container. The one-line
    wiring is safe.

    What the fix here actually is, since "one import line" would also have been the wrong
    shape: ``exchange.ranking.serving`` resolves the ranker's collaborators off ``app.state``
    with the same fail-closed defaults the auction route already uses (an empty trust snapshot
    denies every store; no registered-domain source vouches for no checkout host),
    ``exchange.ranking.candidates`` projects the auction's ``BidEntry`` objects into candidate
    records by NAMING their fields — never passing a store's own bid through, which would let
    a bidder write its own ``intent_match`` — and ``exchange.ranking.routes`` serves the
    published ``GET /auctions/{auction_id}/shortlist``. ``POST /auctions`` ranks at close.

    Neo4j is NOT in that path, and it was worth measuring rather than assuming. The wiring
    grows the served import closure by seventeen modules — the six of ``exchange.retrieval``
    and eleven ``ingest.*`` (counted, because a first draft of this line said "seven" and
    contradicted the "eleven ``ingest.*`` modules" the paragraph above it already stated) —
    because ``filters.py:39`` imports ``retrieval.criteria`` and importing
    a submodule executes ``retrieval/__init__.py``, which imports ``sources`` and ``service``
    too. (An earlier version of this paragraph said ``criteria`` was reached "only, never
    sources/service", which is false and is the kind of false that stops the next reader
    checking.) None of those seventeen touches the network at import: the neo4j driver is
    imported INSIDE ``graph/reembed.py:386``'s ``graph_driver()`` and sentence-transformers
    inside ``embeddings/local_bge.py:93``'s ``_load()``, so ``create_app()`` loads neither
    ``neo4j`` nor ``numpy`` — measured on the built app. Reproduced from the other direction
    with a meta-path finder that raises on any ``neo4j`` import and ``socket`` disabled:
    ``rank()`` ran to completion and applied its R12/R19/C10 exclusions. A Neo4j session, the
    driver pin and ``NEO4J_*`` env are what ``exchange.retrieval`` needs to FUNCTION (T-260),
    not what this gate is about.
    """
    modules = _exchange_app_import_closure()
    ranking = sorted(name for name in modules if name.split(".")[:2] == ["exchange", "ranking"])
    assert ranking, (
        "building the exchange app imports no exchange.ranking module, so the published "
        "ranking — rank(), the hard-constraint filters and the shortlist builder — is on no "
        f"served path; the app's exchange import closure is {sorted(modules)}"
    )


# =====================================================================================
# T-270 — the 422 the request validator builds is itself unserialisable
# =====================================================================================

#: Every JSON literal a caller can put on the wire that ``json.loads`` accepts and
#: ``json.dumps`` refuses. ``NaN``/``Infinity`` are Python's JSON extension, which starlette's
#: ``Request.json()`` reads by default; ``1e400`` is *ordinary, RFC-legal JSON* that overflows
#: to ``inf`` on parse, so a deployment that "just rejects NaN at the parser" still ships it.
T270_NON_FINITE_LITERALS: tuple[str, ...] = ("NaN", "Infinity", "-Infinity", "1e400", "-1e400")

#: The denial codes ``accept_bid`` can answer only AFTER ``_find_bid`` has returned a bid.
#: Used by T-294; declared here beside the accept-route knowledge it belongs to.
T294_CODES_REACHED_ONLY_AFTER_THE_BID_IS_FOUND: frozenset[str] = frozenset(
    {
        "already_accepted",
        "checkout_refused",
        "blacklisted",
        "unavailable",
        "unrecordable_acceptance",
    }
)

_T270_CORPUS: list[dict[str, Any]] | None = None
_T293_CORPUS: list[dict[str, Any]] | None = None
_T294_CORPUS: dict[str, Any] | None = None


def _drawn_seed() -> int:
    """A seed drawn from the OS, never pinned in the source.

    A randomized property with a PINNED seed is a parametrized probe in a costume: the draws
    become an enumerable table, and this project has measured 44+ distinct keys that game
    exactly that shape. The seed is drawn per run and REPORTED in every failure message, so a
    red run stays reproducible without the corpus ever being enumerable in advance.
    """
    import random  # noqa: PLC0415 - kept out of this file's frozen import head

    return random.SystemRandom().randrange(2**32)


def _t270_auction_shape(rng: Any) -> dict[str, Any]:
    """A well-formed ``POST /auctions`` body whose SHAPE is drawn, not just its values.

    Randomising the values alone would leave the set of JSON *positions* fixed, and a fix that
    hardened the four positions a fixed template happens to contain would close the gate while
    leaving the rest of the request surface open. The row count, the constraint count and the
    presence of the two optional top-level fields all vary, so the position set varies with it.
    """
    rows = [
        {
            "store_id": f"store-{rng.randrange(10**6)}",
            "tier": rng.randint(0, 3),
            "product_ref": f"prod-{rng.randrange(10**4)}",
            "list_price": round(rng.uniform(5.0, 500.0), 2),
            "max_discount_pct": round(rng.uniform(1.0, 40.0), 1),
        }
        for _ in range(rng.randint(1, 4))
    ]
    body: dict[str, Any] = {
        "intent": {
            "intent_id": f"intent-{rng.randrange(10**6)}",
            "cluster_id": f"cluster-{rng.randrange(10**4)}",
            "hard_constraints": [
                {
                    "field": rng.choice(("capacity_l", "weight_kg", "volume_ml")),
                    "op": ">=",
                    "value": rng.randint(1, 90),
                }
                for _ in range(rng.randint(0, 2))
            ],
        },
        "roster": rows,
    }
    if rng.random() < 0.5:
        body["profile"] = {"buyer_id": f"buyer-{rng.randrange(10**5)}"}
    if rng.random() < 0.5:
        body["bid_timeout_seconds"] = round(rng.uniform(0.5, 9.0), 2)
    return body


def _t270_positions(node: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Every JSON position in ``node``, the empty tuple (the whole body) included.

    Walking the document rather than naming fields is what makes the battery type-agnostic:
    it lands on ``str`` fields, ``int`` fields, ``float`` fields, the ``dict`` that is
    ``intent``, the ``list`` that is ``roster``, and the root — without this file having to
    know the request model. A repair confined to the numeric fields cannot narrow it.
    """
    found = [prefix]
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_t270_positions(value, (*prefix, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_t270_positions(value, (*prefix, index)))
    return found


def _t270_at(node: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        node = node[key]
    return node


#: How a non-finite literal is wrapped before being planted. ``bare`` is the scalar case;
#: the other two BURY it inside a container, which is what forces the error renderer to
#: recurse rather than glance at a top-level value.
#:
#: This exists because an adversarial pass shipped a deliberately NON-recursive repair — one
#: that sanitises only a scalar ``input`` and walks nothing — and the gate went green. Every
#: case the corpus drew put the non-finite at the top of the echoed value, so recursion was
#: never actually required. A handler that stops at depth one now fails on two thirds of the
#: battery.
T270_WRAPPINGS: tuple[str, ...] = ("bare", "in_list", "in_object")

_T270_SENTINEL = "__T270_PLANTED__"


def _t270_plant(template: Any, path: tuple[Any, ...], literal: str, wrapping: str) -> str:
    """The request bytes with ``literal`` written at ``path``, wrapped per ``wrapping``.

    Built as text, not through ``json.dumps(..., allow_nan=True)`` on a Python ``float('nan')``,
    because the *client* is the attacker here: an httpx/requests caller that hands ``json=``
    a Python NaN is refused by its own serialiser and never reaches the server. Anyone with a
    socket can write the four bytes ``NaN``.
    """
    import copy  # noqa: PLC0415 - kept out of this file's frozen import head

    if wrapping == "in_list":
        planted = f"[1.0, {literal}, 2.0]"
    elif wrapping == "in_object":
        planted = f'{{"depth_one": {{"depth_two": [{literal}]}}}}'
    else:
        planted = literal

    if not path:
        return planted
    body = copy.deepcopy(template)
    node = body
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = _T270_SENTINEL
    return json.dumps(body).replace(f'"{_T270_SENTINEL}"', planted)


def _t270_corpus() -> list[dict[str, Any]]:
    """One shared corpus, built once, consumed by BOTH the armer and the gate.

    Two functions each generating "the same" corpus is the shape that lets an arming test be
    green about a different set of cases than the one the gate iterates. There is one list and
    both tests read it.
    """
    global _T270_CORPUS
    if _T270_CORPUS is not None:
        return _T270_CORPUS

    import random  # noqa: PLC0415 - kept out of this file's frozen import head

    seed = _drawn_seed()
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    for _ in range(4):
        template = _t270_auction_shape(rng)
        for path in _t270_positions(template):
            literal = rng.choice(T270_NON_FINITE_LITERALS)
            wrapping = rng.choice(T270_WRAPPINGS)
            cases.append(
                {
                    "seed": seed,
                    "route": "POST /auctions",
                    "url": "/auctions",
                    "template": template,
                    "path": path,
                    "kind": type(_t270_at(template, path)).__name__,
                    "literal": literal,
                    "wrapping": wrapping,
                    "raw": _t270_plant(template, path, literal, wrapping),
                }
            )
        # The second route matters on its own: ``AcceptBidRequest`` has exactly one property
        # and it is a ``string``, so a repair that introspects numeric fields cannot reach it.
        accept_template = {"bid_ref": f"bid-{rng.randrange(10**6)}"}
        for path in _t270_positions(accept_template):
            for wrapping in T270_WRAPPINGS:
                literal = rng.choice(T270_NON_FINITE_LITERALS)
                cases.append(
                    {
                        "seed": seed,
                        "route": "POST /auctions/{auction_id}/accept",
                        "url": f"/auctions/auction-{rng.randrange(10**6)}/accept",
                        "template": accept_template,
                        "path": path,
                        "kind": type(_t270_at(accept_template, path)).__name__,
                        "literal": literal,
                        "wrapping": wrapping,
                        "raw": _t270_plant(accept_template, path, literal, wrapping),
                    }
                )
    _T270_CORPUS = cases
    return cases


def _t270_clients() -> list[tuple[str, Any]]:
    """BOTH exchange apps: the module-level ASGI object uvicorn serves, and a fresh build.

    Testing only ``create_app()`` is a measured hole, and an adversarial pass found it by
    exploiting it. ``apps/exchange/src/main.py:52`` is a module-level ``app = create_app()``,
    executed at import time — BEFORE the feature modules are imported — and FastAPI binds its
    default exception handlers by ``setdefault`` in ``__init__``. So a repair that installs a
    handler while a feature module is being imported (the only shape available inside the
    ticket's one-file scope) is picked up by every app built AFTERWARDS and by none built
    before. Probing a fresh ``create_app()`` alone therefore reports that repair as working
    while ``uvicorn exchange.main:app`` — the literal object the Dockerfile's CMD names —
    still answers 500 to an anonymous caller. Both are asked, and the failure message says
    which one answered.

    ``raise_server_exceptions=False`` so the transport answers 500 the way uvicorn does,
    instead of re-raising into the test and turning a served 500 into an ERROR.

    **This function measures only the fresh build; the served object is measured in a
    subprocess** by :func:`_t270_served_app_statuses`, and that split is not fussiness. Whether
    the in-process ``exchange.main.app`` was built before or after a feature module is a
    property of THIS PROCESS'S IMPORT HISTORY, not of the code under test. Measured: running
    this gate alone is red, but running ``test_t294_… test_t270_…`` together turned it GREEN
    under a repair that leaves the deployed app broken — because ``_t294_corpus`` imports
    ``exchange.auction.routes`` first, so an import-time patch landed before ``main.py:52``
    executed. The full-directory run survived only by alphabetical accident. A subprocess that
    imports ``exchange.main`` first, with nothing else loaded, is the only way to ask the
    question the deployment actually poses.
    """
    from exchange.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    return [("exchange.main.create_app()", TestClient(create_app(), raise_server_exceptions=False))]


def _t270_served_app_statuses(cases: list[dict[str, Any]]) -> list[int]:
    """POST every corpus case at ``exchange.main:app`` in a FRESH interpreter, and report status.

    The subprocess imports ``exchange.main`` as its first repo import, so the module-level
    ``app = create_app()`` at ``main.py:52`` runs exactly as it does under
    ``uvicorn exchange.main:app`` — before any feature module has been imported by anything
    else. In-process this is unmeasurable: pytest has already imported half the tree, and the
    answer changes with collection order.
    """
    import os  # noqa: PLC0415 - kept out of this file's frozen import head
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    repo_root = Path(__file__).resolve().parents[3]
    payload = json.dumps([{"url": case["url"], "raw": case["raw"]} for case in cases])
    code = (
        "import json, sys\n"
        "import exchange.main\n"
        "from fastapi.testclient import TestClient\n"
        "client = TestClient(exchange.main.app, raise_server_exceptions=False)\n"
        "cases = json.loads(sys.stdin.read())\n"
        "out = []\n"
        "for case in cases:\n"
        "    try:\n"
        "        response = client.post(case['url'], content=case['raw'].encode(),\n"
        "                               headers={'content-type': 'application/json'})\n"
        "        out.append(response.status_code)\n"
        "    except Exception:\n"
        "        out.append(599)\n"
        "print(json.dumps(out))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), str(repo_root / ".pkgroot")])
    env["PROXYSHOP_WORKER"] = env.get("PROXYSHOP_WORKER", "0")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, (
        f"the served-app probe would not run:\n{completed.stderr[-3000:]}"
    )
    return list(json.loads(completed.stdout.strip().splitlines()[-1]))


def test_the_non_finite_payload_corpus_is_armed() -> None:
    """NOT xfail, and the separation is the whole reason it exists.

    Under ``xfail(strict=True)`` **any** exception in a test body is reported ``xfailed``,
    which is green — so a corpus builder that had quietly stopped producing cases would be
    indistinguishable from the defect it is supposed to detect, and under the ticket's own
    ``--runxfail`` gate a dead builder reads as "still broken" forever. Everything that could
    make the battery measure nothing is asserted here, in its own name.

    The last check is the one that matters most and is the easiest to leave out: **the
    unmodified template must be accepted**. Every assertion in the gate is ``status < 500``,
    and a template that had drifted out of the request schema would answer 422 to every case
    — under 500, so the gate would go GREEN with the defect fully alive. A corpus that cannot
    produce a 201 is not measuring the error path; it is measuring its own staleness.
    """
    cases = _t270_corpus()
    clients = _t270_clients()

    assert len(cases) >= 60, f"the corpus built only {len(cases)} cases; the sweep is unarmed"

    assert len(clients) == 1, (
        f"the in-process client list holds {len(clients)} entries; the served object is "
        "measured by subprocess, not here"
    )

    # The served-app probe must actually run and actually see the defect's shape. A probe that
    # returned 599 for everything (its own exception sentinel) would make the gate red for the
    # wrong reason forever, and one that returned nothing would make it green.
    control_statuses = _t270_served_app_statuses(cases[:5])
    assert len(control_statuses) == 5, (
        f"the served-app subprocess returned {control_statuses}; it is not measuring the corpus"
    )
    assert all(status != 599 for status in control_statuses), (
        f"the served-app subprocess could not dispatch at all (599 is its own exception "
        f"sentinel): {control_statuses}"
    )

    routes = {case["route"] for case in cases}
    assert routes == {"POST /auctions", "POST /auctions/{auction_id}/accept"}, (
        f"the corpus covers only {sorted(routes)}; the defect is in the shared error path and "
        "a battery aimed at one route cannot see a repair that is scoped to the other"
    )

    literals = {case["literal"] for case in cases}
    assert len(literals) >= 4, (
        f"only {sorted(literals)} were planted; a fix that rejects NaN at the parser still "
        "ships 1e400, which is RFC-legal JSON that overflows to inf"
    )

    kinds = {case["kind"] for case in cases}
    assert {"str", "int", "float", "dict", "list"} <= kinds, (
        f"the corpus plants non-finite values only into {sorted(kinds)} positions — the "
        "defect is type-agnostic (it is in the error RENDERER, not in any field), so a "
        "battery that only reaches numeric fields certifies a repair that closes four fields"
    )

    paths = {(case["route"], case["path"]) for case in cases}
    assert len(paths) >= 12, f"only {len(paths)} distinct positions are covered; too narrow"

    wrappings = {case["wrapping"] for case in cases}
    assert set(T270_WRAPPINGS) <= wrappings, (
        f"the corpus only plants {sorted(wrappings)}; without `in_list` and `in_object` the "
        "non-finite always sits at the top of the echoed value, so a repair that sanitises a "
        "scalar and recurses into nothing passes — measured, that exact non-recursive handler "
        "turned this gate green"
    )

    # EVERY drawn template, on EVERY app, not just the first. An earlier version of this loop
    # `break`ed after one case, so it validated one of the four templates the corpus draws —
    # three quarters of the battery could have gone stale invisibly.
    checked = 0
    seen: list[Any] = []
    for case in cases:
        if case["route"] != "POST /auctions" or case["template"] in seen:
            continue
        seen.append(case["template"])
        for label, client in clients:
            control = client.post("/auctions", json=case["template"])
            checked += 1
            assert control.status_code == 201, (
                f"on {label} the corpus's own unmodified template answered "
                f"{control.status_code}, not 201 — seed {case['seed']}, body "
                f"{json.dumps(case['template'])[:400]}, response {control.text[:400]}. Every "
                "gate assertion is `status < 500`, so a stale template makes the gate pass on "
                "422s without the defect being touched."
            )
    assert checked >= 4, (
        f"only {checked} unmodified-template controls ran; the corpus draws four shapes and "
        "each must be shown to be a request the app still accepts"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-270: a non-finite JSON literal anywhere in an unauthenticated request body returns "
        "HTTP 500. pydantic rejects the value correctly, and then FastAPI's "
        "request_validation_exception_handler builds a 422 body that ECHOES the offending "
        "input back, where json.dumps raises 'Out of range float values are not JSON "
        "compliant'. The defect is the error RENDERER, so it fires on fields of every type "
        "(str, int, float, dict, list) and on both served POST routes; remove this marker "
        "with the fix"
    ),
)
def test_t270_no_field_of_any_request_can_produce_a_5xx() -> None:
    """Nothing an anonymous caller can write may make the exchange answer 5xx.

    Measured at HEAD over ``TestClient(create_app(), raise_server_exceptions=False)``, sending
    raw bytes rather than ``json=`` (the client's own serialiser refuses a Python NaN, which
    is why this went unnoticed — you have to be a *socket*, not an SDK, to reach it)::

        list_price: NaN            -> 500      store_id: NaN     (a `str` field)   -> 500
        list_price: Infinity       -> 500      product_ref: NaN  (a `str` field)   -> 500
        list_price: 1e400          -> 500      intent: NaN       (a `dict` field)  -> 500
        tier: NaN   (an `int`)     -> 500      roster: NaN       (a `list` field)  -> 500
        tier: 1e400 (an `int`)     -> 500      profile: NaN                        -> 500
        max_discount_pct: NaN      -> 500      the whole body is `NaN`             -> 500
        POST /auctions/{id}/accept with `bid_ref: NaN`  (its ONLY field, a string) -> 500

    and on one 84-case draw of this corpus, **57 answered 500**.

    Two things about that table decide the shape of this gate, and both were established by
    measurement rather than by reading the ticket.

    **The ticket names ``list_price`` and ``tier``, and that is not where the defect lives.**
    The 500 does not come from the handler; it comes from the shared validation-error
    renderer, which echoes ``input`` back into the 422 body for whichever field failed. So a
    repair scoped to ``apps/exchange/src/auction/routes.py`` — which is the ticket's whole
    ``scope``, and ``apps/exchange/src/main.py`` is frozen against worker edits — that swaps
    the four numeric fields for an ``Annotated`` type with a sanitising ``BeforeValidator``
    would answer 422 for every numeric case and leave ``store_id: NaN``, ``intent: NaN`` and
    the entire ``accept`` route still returning 500 to anyone with a socket. That repair must
    NOT turn this gate green, so the battery is built by walking the request document and
    planting at every position it finds, on both served POST routes, instead of by naming
    fields. The arming test refuses a corpus that does not reach ``str``, ``int``, ``float``,
    ``dict`` and ``list`` positions, which is the property a field-local fix cannot satisfy.

    **What is deliberately NOT asserted: that a non-finite value must be REJECTED.** Two
    positions answer 201 today and are correct to. ``bid_timeout_seconds: NaN`` -> 201 is
    ``bid_window_seconds`` doing exactly what its docstring says ("``nan`` compares false
    against everything, so ``max(0.0, nan)`` is ``0.0`` … a garbage timeout becomes 'no
    window', never 'an infinite one'"), and a non-finite nested inside ``intent`` -> 201 is
    ``dict[str, Any]`` doing what its annotation says. Asserting 4xx-not-2xx there would gate
    a preference and would make this test red for a reason the ticket is not about. The
    ticket's property is exactly one sentence — "Nothing the caller can write may produce a
    5xx" — and that is the only thing asserted.

    Note the F1 fix (``allow_inf_nan=False``) does not close this and makes it strictly
    easier to reach: it turns a non-finite into a *rejection*, and rejection is the path that
    500s.
    """
    cases = _t270_corpus()
    clients = _t270_clients()

    assert len(cases) >= 60, (
        f"the shared corpus holds {len(cases)} cases; this gate cannot conclude anything from "
        "a battery that was not built (see test_the_non_finite_payload_corpus_is_armed)"
    )

    failures: list[str] = []
    probed = 0

    def _record(label: str, case: dict[str, Any], status: int) -> None:
        if status < 500:
            return
        where = ".".join(str(part) for part in case["path"]) or "<the whole body>"
        failures.append(
            f"[{label}] {case['route']} with {case['literal']} at {where} "
            f"(a {case['kind']} position) -> {status}"
        )

    for label, client in clients:
        for case in cases:
            response = client.post(
                case["url"],
                content=case["raw"].encode(),
                headers={"content-type": "application/json"},
            )
            probed += 1
            _record(label, case, response.status_code)

    # The object uvicorn serves, measured in a clean interpreter so the answer does not depend
    # on what this pytest process happened to import first.
    served_statuses = _t270_served_app_statuses(cases)
    assert len(served_statuses) == len(cases), (
        f"the served-app probe returned {len(served_statuses)} statuses for {len(cases)} cases; "
        "it is not measuring the corpus"
    )
    for case, status in zip(cases, served_statuses, strict=True):
        probed += 1
        _record("exchange.main:app (the served ASGI object)", case, status)

    assert probed == len(cases) * (len(clients) + 1), (
        f"{probed} probes ran, expected {len(cases) * (len(clients) + 1)}; the sweep is unarmed"
    )

    assert not failures, (
        f"{len(failures)} of {probed} unauthenticated requests were answered 5xx by the "
        f"exchange (corpus seed {cases[0]['seed']}); a caller that can open a socket can make "
        "the service fail, and the failure is in the error renderer rather than in any one "
        "field. The label on each line says WHICH app answered — a repair that reaches only "
        "`create_app()` and not the module-level `exchange.main:app` has not shipped, because "
        f"`app = create_app()` runs at import time before any feature module:\n  "
        + "\n  ".join(sorted(failures)[:25])
    )


# =====================================================================================
# T-293 — a live memory address reaches an unauthenticated client and a persisted event
# =====================================================================================

#: A pointer as CPython renders one in ``object.__repr__``. Six hex digits, so a short hash or
#: a colour like ``0xfff`` is not mistaken for an address.
T293_ADDRESS = r"0x[0-9a-fA-F]{6,}"


def _t293_corpus() -> list[dict[str, Any]]:
    """Drive ``POST /auctions/{id}/accept`` with a differently-named unusable creator each time.

    The creator classes are BUILT here, with names drawn at run time, and none of them defines
    ``__repr__`` — so each instance renders through ``object.__repr__`` and carries its own
    address. Generating the class rather than reusing ``object()`` is what lets the gate assert
    the *positive* half: the class's own name must survive the repair, which distinguishes
    ``describe()`` (renders ``<T293Creator_QWERTY>``) from "delete the object from the message",
    which would satisfy a pure no-address assertion perfectly while destroying the diagnostic.
    """
    global _T293_CORPUS
    if _T293_CORPUS is not None:
        return _T293_CORPUS

    import random  # noqa: PLC0415 - kept out of this file's frozen import head
    import string  # noqa: PLC0415

    from exchange.accept import accept  # noqa: PLC0415
    from exchange.accept.routes import InMemoryAuctionBids, configure_accept  # noqa: PLC0415
    from exchange.auction.routes import configure_auctions  # noqa: PLC0415
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility  # noqa: PLC0415
    from exchange.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    seed = _drawn_seed()
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    for index in range(12):
        label = "T293Creator_" + "".join(rng.choice(string.ascii_uppercase) for _ in range(8))
        creator = type(label, (), {})()
        store_id = f"store-{rng.randrange(10**6)}"
        domain = f"{store_id}.example"

        app = create_app()
        bids = InMemoryAuctionBids()
        configure_auctions(app, eligibility=StaticSellerEligibility({store_id: ELIGIBLE}))
        configure_accept(
            app,
            registered_domains=lambda _store_id, _domain=domain: _domain,
            eligibility=StaticSellerEligibility({store_id: ELIGIBLE}),
            code_creator=creator,
            checkout_mode="shopify",
            bids=bids,
        )
        client = TestClient(app, raise_server_exceptions=False)
        opened = client.post(
            "/auctions",
            json={
                "intent": {"intent_id": f"intent-{index}", "cluster_id": "cluster-1"},
                "roster": [
                    {
                        "store_id": store_id,
                        "tier": 1,
                        "list_price": 100.0,
                        "product_ref": "prod-1",
                        "max_discount_pct": 20.0,
                    }
                ],
            },
        )
        auction_id = opened.json().get("auction_id", "")
        bids.record(
            auction_id,
            [
                {
                    "bid_ref": "bid-a",
                    "bid_id": "bid-a",
                    "store_id": store_id,
                    "store_domain": domain,
                    "unit_price": 90.0,
                    "total_price": 90.0,
                    "offer": {"product_ref": "prod-1", "unit_price": 90.0, "total_price": 90.0},
                }
            ],
        )
        refused = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})

        # The DURABLE half, measured separately. The ticket says the address reaches an
        # unauthenticated HTTP client AND a persisted event, and only the first was being
        # captured — an adversarial pass exploited exactly that gap, scrubbing `0x…` inside
        # `accept/routes.py:_denied` (the HTTP boundary) and leaving the policy_event's
        # `reason` untouched, address included. Gate green, pointer still written down
        # forever. `accept()` is called directly here because the event is not on the wire.
        persisted = ""
        try:
            direct = accept(
                {
                    "auction_id": auction_id,
                    "intent_id": f"intent-{index}",
                    "cluster_id": "cluster-1",
                    "accepted_bid_ref": None,
                    "now": 1_700_000_000.0,
                    "bids": [
                        {
                            "bid_id": "bid-a",
                            "store_id": store_id,
                            "store_domain": domain,
                            "offer": {
                                "product_ref": "prod-1",
                                "unit_price": 90.0,
                                "total_price": 90.0,
                                "checkout_url": f"https://{domain}/cart/1:1",
                                "expires_at": 2_000_000_000.0,
                            },
                        }
                    ],
                },
                "bid-a",
                creator,
                "shopify",
                registered_domains=lambda _store_id, _domain=domain: _domain,
            )
            payloads = [
                event["payload"] for event in direct.events if event["kind"] == "policy_event"
            ]
            persisted = str(payloads[0].get("reason", "")) if payloads else ""
        except Exception as exc:  # pragma: no cover - reported by the armer, never swallowed
            persisted = f"<the persisted-event probe raised {type(exc).__name__}: {exc}>"

        cases.append(
            {
                "seed": seed,
                "class_name": label,
                "opened_status": opened.status_code,
                "status": refused.status_code,
                "body": refused.text,
                "denial_reason": str(refused.json().get("denial_reason", "")),
                "persisted_reason": persisted,
            }
        )
    _T293_CORPUS = cases
    return cases


def test_the_unusable_code_creator_corpus_is_armed() -> None:
    """NOT xfail: everything that could make the battery grade nothing, asserted in its own name.

    The failure this exists to prevent is specific and was named by the refuter that attacked
    the first draft of this gate. ``checkout_refused`` is emitted from exactly ONE site — the
    broad ``except`` around ``provider.checkout(request)`` at ``accept/offer.py:512`` — and
    that site's own comment says it stays deliberately broad: "an off-domain checkout URL, an
    unusable offer field, a merchant ``POST /codes`` that answered with no code or did not
    answer at all". So a fixture that drifts — a missing ``registered_domains``, an offer with
    no ``product_ref`` — produces ``checkout_refused: CheckoutDomainError: …`` or
    ``checkout_refused: KeyError: 'product_ref'``, which contains no address, mentions no
    creator, and would satisfy a bare "no address in the body" assertion **perfectly, while
    never reaching providers.py:110 at all**. The randomisation actively disguises that: twelve
    generated classes and twelve refusals read as thorough coverage even when every one of them
    ran past the code under test.

    So the arming half checks that the auction opened at all, that the accept was refused as a
    409 rather than lost to a 404/503, and that the twelve refusals are DISTINCT — which is the
    cheapest available proof that the rendering still depends on the creator object. The gate
    itself then asserts the branch by name.
    """
    cases = _t293_corpus()

    assert len(cases) == 12, f"the corpus built {len(cases)} cases, not 12; it is unarmed"

    for case in cases:
        assert case["opened_status"] == 201, (
            f"POST /auctions answered {case['opened_status']} while building the corpus, so "
            f"there was no auction to accept against (seed {case['seed']})"
        )
        assert case["status"] == 409, (
            f"the accept answered {case['status']}, not the 409 that carries denial_reason — "
            f"a 404 or 503 means the fixture, not the checkout branch, decided this case "
            f"(seed {case['seed']}): {case['body'][:300]}"
        )
        assert case["denial_reason"].startswith("checkout_refused"), (
            f"the refusal was {case['denial_reason'][:200]!r}, which is not a checkout "
            f"refusal at all (seed {case['seed']})"
        )
        # The persisted-event probe must have produced a real refusal, not an exception it
        # swallowed. Without this, a broken direct-`accept()` call would silently make the
        # gate's durable half unfalsifiable — the empty string matches no address.
        assert case["persisted_reason"].startswith("checkout_refused"), (
            "the persisted policy_event probe did not produce a checkout refusal, so the "
            "gate's durable half is grading nothing: "
            f"{case['persisted_reason'][:250]!r} (seed {case['seed']})"
        )

    # Distinctness is measured with the ADDRESSES STRIPPED, and that is not a detail: an
    # adversarial pass found the previous version circularly armed. Comparing raw messages,
    # twelve creators that all render as the SAME class name are still "distinct" today,
    # because the leaked pointer differs per instance — so the check passed for exactly the
    # reason the gate exists to remove, and only began working after the defect was fixed.
    # Stripping `0x…` first makes it measure what it claims: dependence on the creator's
    # identity, not on where the allocator happened to put it.
    import re  # noqa: PLC0415 - kept out of this file's frozen import head

    address = re.compile(T293_ADDRESS)
    reasons = {address.sub("0xREDACTED", case["denial_reason"]) for case in cases}
    assert len(reasons) == len(cases), (
        f"the {len(cases)} refusals collapse to {len(reasons)} distinct messages once memory "
        "addresses are masked, so the message no longer depends on WHICH creator was injected "
        "— the corpus can no longer tell a repair that redacts the address from one that "
        f"deleted the object entirely (seed {cases[0]['seed']})"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-293: checkout/providers.py:110 formats an arbitrary injected code creator with "
        "`{creator!r}`, so a misconfigured `code_creator` puts a live CPython memory address "
        "into the 409 body an unauthenticated caller reads AND into the persisted "
        "policy_event. This is T-264's defect on the path T-264 did not cover; the same file "
        "already fixed the class for T-215 two lines further down; remove this marker with "
        "the fix"
    ),
)
def test_t293_an_unusable_code_creator_does_not_render_a_memory_address() -> None:
    """A refusal may name the SHAPE of what it refused; it may not publish a pointer.

    ``ShopifyCheckoutProvider.mint`` raises at ``apps/exchange/src/checkout/providers.py:109``::

        raise CheckoutCreatorError(
            f"injected code creator {creator!r} exposes neither create_code(...) "
            f"nor __call__(...)"
        )

    ``accept()`` catches it in the broad ``except`` at ``accept/offer.py:512`` and builds
    ``denial_reason(DENIAL_CHECKOUT_REFUSED, f"{type(exc).__name__}: {exc}")`` at ``:541``, so
    the address travels inside the DECLARED vocabulary intact — the T-204 boundary normaliser
    does not strip it, because ``checkout_refused`` *is* a declared code and only the prose
    carries the pointer. Measured live at HEAD, verbatim 409 body::

        {"accepted": false, "denial_reason": "checkout_refused: CheckoutCreatorError:
         injected code creator <object object at 0x104821d80> exposes neither
         create_code(...) nor __call__(...)"}

    and the identical string, address included, in the persisted ``policy_event`` payload's
    ``reason`` — measured by calling ``accept()`` directly and reading
    ``[e['payload'] for e in result.events if e['kind'] == 'policy_event'][0]['reason']``.
    Contrast the same call with a bad ``registered_domains`` instead: ``checkout_refused:
    TypeError: registered_domains of type 'dict' exposes neither domain_for(store_id) nor
    __call__(store_id)`` — no address. That is the T-264 repair working on the path it covered
    and absent from this one. Two lines below the defect, at ``providers.py:126``, the same
    file already renders a merchant reply by its *shape* rather than its value (T-215), so the
    pattern is in the file; line 110 was simply missed.

    **Three assertions, and the two positive ones are load-bearing.** A gate that only said
    "no ``0x…`` in the body" would be satisfied by a run that never entered this branch, and
    equally by a repair that deleted the creator from the message altogether — which destroys
    the only thing an operator debugging a misconfigured deployment needs. So:

    * the message must still say ``exposes neither create_code``, which is what proves
      ``providers.py``'s creator branch was ENTERED rather than some unrelated
      ``checkout_refused``;
    * no ``0x[0-9a-fA-F]{6,}`` anywhere in the body an anonymous client reads;
    * the creator's own class name must survive. ``exchange.accept.reasons.describe`` — the
      helper T-264 added for exactly this, at ``accept/reasons.py:111`` — renders a non-scalar
      as ``f"<{type(value).__name__}>"``, so ``<T293Creator_QWERTYUI>`` passes and keeps the
      diagnostic. Note ``describe()`` currently lives under ``apps/exchange/src/accept/`` and
      ``checkout/`` must not import from ``accept/``; check ``.importlinter`` before choosing
      where the helper lands. This gate does not care which module it ends up in.
    """
    import re  # noqa: PLC0415 - kept out of this file's frozen import head

    cases = _t293_corpus()
    address = re.compile(T293_ADDRESS)

    assert len(cases) == 12, (
        f"the shared corpus holds {len(cases)} cases; this gate concludes nothing from a "
        "battery that was not built (see test_the_unusable_code_creator_corpus_is_armed)"
    )

    entered = [case for case in cases if "exposes neither create_code" in case["denial_reason"]]
    assert len(entered) == len(cases), (
        f"only {len(entered)} of {len(cases)} refusals came from the creator branch at "
        "checkout/providers.py — the rest are some other checkout_refused, so this gate would "
        "be grading a path that never touches the defect: "
        f"{[case['denial_reason'][:120] for case in cases if case not in entered][:3]}"
    )

    leaked = [case for case in cases if address.search(case["body"])]
    assert not leaked, (
        f"{len(leaked)} of {len(cases)} unauthenticated 409 bodies carry a live CPython "
        f"memory address (corpus seed {cases[0]['seed']}):\n  "
        + "\n  ".join(case["denial_reason"][:200] for case in leaked[:3])
    )

    # The DURABLE half, asserted separately from the published one and NOT as a restatement
    # of it. An adversarial pass scrubbed `0x…` at the HTTP boundary in `_denied` and left the
    # policy_event untouched: the gate went green, the whole apps/exchange suite stayed green,
    # and the pointer was still being written into a persisted event forever. The ticket names
    # both halves; both are now measured, and a boundary-only redaction fails here.
    durable = [case for case in cases if address.search(case["persisted_reason"])]
    assert not durable, (
        f"{len(durable)} of {len(cases)} persisted policy_event reasons carry a live CPython "
        f"memory address (corpus seed {cases[0]['seed']}). Redacting only the HTTP response "
        "leaves the pointer in the durable record, which is the half an operator reads back "
        "months later; the fix belongs where the message is BUILT "
        "(checkout/providers.py), not at the boundary:\n  "
        + "\n  ".join(case["persisted_reason"][:200] for case in durable[:3])
    )

    forgotten = [case for case in cases if case["class_name"] not in case["body"]]
    assert not forgotten, (
        f"{len(forgotten)} refusals no longer name the injected creator's type at all. "
        "Deleting the object from the message does satisfy the no-address half, and it also "
        "removes the only thing that tells an operator WHICH misconfigured object their "
        "deployment is carrying. `exchange.accept.reasons.describe` renders "
        "`<ClassName>` and keeps both properties:\n  "
        + "\n  ".join(
            f"{case['class_name']}: {case['denial_reason'][:160]}" for case in forgotten[:3]
        )
    )


# =====================================================================================
# T-294 — the exchange returns bids it then refuses to accept, because it stores none
# =====================================================================================


def _t294_corpus() -> dict[str, Any]:
    """Run real auctions through the served app and keep the bids it answered with.

    The wiring here is deliberately the MINIMUM that produces real bids, and it deliberately
    leaves the bid store alone. ``configure_auctions`` is given an eligibility source and a
    solicitor — the two things any deployment must supply, and without which every store is
    denied ``unavailable`` and ``entries`` comes back empty. ``configure_accept`` IS called,
    with everything a real deployment must supply — a registered-domain source, an
    eligibility source and a ``CHECKOUT_MODE`` — and **deliberately without ``bids=``**, which
    is the one seam under test. So ``app.state.auction_bids`` keeps its fail-closed default,
    ``NoRecordedBids``, exactly as ``uvicorn exchange.main:app`` runs it.

    **Wiring the rest of the deployment is not incidental; it is what gives this gate any
    discriminating power at all, and an adversarial pass proved the earlier version had almost
    none.** With ``configure_accept`` omitted, ``registered_domains`` stays fail-closed, so
    every checkout is off-domain and the accept is refused ``checkout_refused`` whether or not
    the bid was ever recorded. A gate whose corpus can never produce a successful acceptance
    can only assert something about the refusal's *code word*, and two edits defeated exactly
    that: relabelling ``DENIAL_UNKNOWN_BID = "unavailable"`` in ``accept/reasons.py`` (one
    line, no behaviour change, gate green), and rebuilding bids from ``roster`` — the shape
    the ticket forbids by name because it re-opens T-169 — which refused off-domain and landed
    on an allowed code. Both are now red, because the corpus can reach a real acceptance and
    the gate demands one.

    ``checkout_mode="redirect"`` is the simulated provider that mints locally and needs no
    merchant client, so the corpus needs no ``code_creator`` and touches no network.

    **The store's bid ref and its domain are per-store random tokens, and that is what makes
    the forbidden roster-rebuild fail.** A first repair of this gate wired the registry as
    ``lambda store_id: f"{store_id}.example"`` and minted refs as ``bid-{store_id}``, and
    claimed in this docstring that a roster-rebuilt bid could not clear the domain check. An
    adversarial pass MEASURED that claim false: a formula-registry vouches for any string
    handed to it without ever reading the bid, so the rebuild minted a live single-use code at
    a list price the store never offered. Both values are now drawn from a token table the
    roster does not contain — ``RosterEntry`` has no bid id and no ``store_domain`` — so a
    rebuilt bid cannot be FOUND, and if it were found it could not clear the domain check.

    The gate still passes for ANY honest wiring: a ``POST /auctions`` that records
    ``[entry.bid for entry in result.entries]`` into whatever bid store the app carries
    (creating one if absent) closes it, and so does threading a store through both
    ``configure_*`` calls. Nothing here prescribes which.
    """
    global _T294_CORPUS
    if _T294_CORPUS is not None:
        return _T294_CORPUS

    import random  # noqa: PLC0415 - kept out of this file's frozen import head

    from exchange.accept.routes import configure_accept  # noqa: PLC0415
    from exchange.auction.routes import configure_auctions  # noqa: PLC0415
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility  # noqa: PLC0415
    from exchange.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    seed = _drawn_seed()
    rng = random.Random(seed)

    known = {f"store-{index}": ELIGIBLE for index in range(1, 80)}

    #: Per-store secrets the ROSTER DOES NOT CONTAIN, and this is the load-bearing part.
    #:
    #: An adversarial pass defeated the previous version of this corpus with the very fix the
    #: ticket forbids by name: rebuilding bids from ``roster``. It worked because both the bid
    #: ref (``bid-{store_id}``) and the domain (``{store_id}.example``) were DERIVABLE FROM THE
    #: STORE ID, so a rebuilt row could reproduce them and mint a live single-use code at a
    #: list price the store never offered. Nothing about the corpus made the real bid special.
    #:
    #: Now the store agent mints a token no caller can guess, and puts it in the two places
    #: that decide an accept: the bid ref ``_find_bid`` matches on, and the domain the platform
    #: registry vouches for. A ``RosterEntry`` carries neither — it has no bid id and no
    #: ``store_domain`` — so a roster-rebuilt bid cannot be found, and if it were found it
    #: could not clear the domain check. The forbidden fix now fails on two independent counts.
    tokens = {store_id: f"{rng.randrange(16**8):08x}" for store_id in known}
    bid_refs = {store_id: f"bid-{token}" for store_id, token in tokens.items()}
    domains = {store_id: f"{token}.example" for store_id, token in tokens.items()}

    class _Solicitor:
        """A store agent that answers honestly, under its own list price.

        The reply envelope is ``{store_id, bid}`` because that is what ``collect_bids`` reads;
        a reply with no ``bid`` mapping is labelled ``response_carried_no_bid`` and becomes a
        list-price FALLBACK, which would empty this corpus of the only entries it cares about.
        """

        def solicit(self, store: Any) -> Any:
            store_id = str(store["store_id"])
            list_price = float(store.get("list_price", 100.0))
            unit = round(list_price * rng.uniform(0.82, 0.97), 2)
            return {
                "store_id": store_id,
                "bid": {
                    "bid_id": bid_refs[store_id],
                    "bid_ref": bid_refs[store_id],
                    "store_id": store_id,
                    "store_domain": domains[store_id],
                    "offer": {
                        "product_ref": store.get("product_ref") or "prod-1",
                        "unit_price": unit,
                        "total_price": unit,
                        "currency": "USD",
                        "expires_at": "2999-01-01T00:00:00Z",
                    },
                    "claims": [],
                },
            }

        __call__ = solicit

    app = create_app()
    configure_auctions(app, eligibility=StaticSellerEligibility(known), solicitor=_Solicitor())
    # A complete deployment EXCEPT `bids=`. Every keyword here is one a real operator must
    # set; the omitted one is the seam the ticket is about, so it keeps `NoRecordedBids`.
    # The registry answers from the SAME token table the store bid from — a real platform
    # registry, not a formula that vouches for any string handed to it.
    configure_accept(
        app,
        registered_domains=lambda store_id: domains.get(str(store_id)),
        eligibility=StaticSellerEligibility(known),
        checkout_mode="redirect",
    )
    client = TestClient(app, raise_server_exceptions=False)

    auctions: list[dict[str, Any]] = []
    for index in range(20):
        chosen = rng.sample(sorted(known), rng.randint(2, 4))
        roster = [
            {
                "store_id": store_id,
                "tier": rng.randint(1, 3),
                "list_price": round(rng.uniform(40.0, 400.0), 2),
                "product_ref": "prod-1",
                "max_discount_pct": round(rng.uniform(10.0, 30.0), 1),
            }
            for store_id in chosen
        ]
        opened = client.post(
            "/auctions",
            json={
                "intent": {"intent_id": f"intent-{index}", "cluster_id": "cluster-1"},
                "roster": roster,
            },
        )
        body = opened.json() if opened.status_code == 201 else {}
        entries = [entry for entry in body.get("entries", []) if not entry.get("fallback")]
        auctions.append(
            {
                "status": opened.status_code,
                "auction_id": body.get("auction_id", ""),
                "denied": body.get("denied", []),
                "roster_size": len(roster),
                # The UNGUESSABLE ref the store agent minted, carried through by
                # `collect_bids` on `BidEntry.bid['bid_id']`. `AuctionEntryOut` publishes no
                # bid ref of its own — a second face of this same gap — so this is the ref a
                # real buyer's agent would present, having been told it by the store. It is
                # deliberately NOT derivable from `store_id`: see the `tokens` table above.
                "bids": [
                    {"store_id": entry["store_id"], "bid_ref": bid_refs[entry["store_id"]]}
                    for entry in entries
                ],
            }
        )

    _T294_CORPUS = {"seed": seed, "client": client, "auctions": auctions}
    return _T294_CORPUS


def test_the_recorded_bid_corpus_is_armed() -> None:
    """NOT xfail: the gate's loop must be arithmetically incapable of running zero times.

    This is the hole the refuter hit on its first attempt at the proposed gate, and it is not
    hypothetical: a completely ordinary bid — 90.0 against a 100.0 list price with a 20%
    cap — comes back ``fallback: True, fallback_reason: 'bid_price_unreconcilable'`` if the
    offer omits ``product_ref``/``total_price``, and the price wall in ``auction/collect.py``
    is under active churn (T-177, T-223, T-224 and T-250 all edited it). Every assertion in
    the gate lives inside a loop over NON-FALLBACK entries, so one tightening there and the
    whole corpus becomes fallbacks and the gate reports green with the defect intact.

    Two devices, together: the corpus is built ONCE by ``_t294_corpus`` and both this test and
    the gate read the same object — never two functions each generating "the same" corpus —
    and the gate repeats the count assertion at the top of its OWN body, so it cannot go quiet
    even if this companion is deselected.
    """
    corpus = _t294_corpus()
    auctions = corpus["auctions"]
    seed = corpus["seed"]

    assert len(auctions) == 20, f"{len(auctions)} auctions were opened, not 20 (seed {seed})"

    for auction in auctions:
        assert auction["status"] == 201, (
            f"POST /auctions answered {auction['status']} (seed {seed}); the corpus is not "
            "measuring the accept path at all"
        )
        assert not auction["denied"], (
            f"the eligibility source denied {auction['denied']} (seed {seed}) — an ineligible "
            "store is never solicited and never appears in `entries`, so a corpus with "
            "denials is measuring the R12 gate rather than the bid store"
        )

    pairs = [(auction["auction_id"], bid) for auction in auctions for bid in auction["bids"]]
    assert len(pairs) >= 40, (
        f"the corpus holds {len(pairs)} non-fallback bids (seed {seed}); every gate assertion "
        "iterates these, so a corpus that has collapsed to fallbacks makes the gate pass "
        "vacuously"
    )

    richest = max(len(auction["bids"]) for auction in auctions)
    assert richest >= 2, (
        f"no auction carried more than {richest} non-fallback bid (seed {seed}); the corpus "
        "is too thin to distinguish 'this bid is unknown' from 'this auction is empty'"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-294 (RESTATED, and the ticket's premise has changed under it). The recording half "
        "is DONE: POST /auctions now writes what it collected into app.state.auction_bids "
        "(auction/routes.py::collected_bid_records), and an auction's own shortlist bid mints "
        "a real code — apps/exchange/tests/test_composition_root.py drives exactly that over a "
        "real socket. What this node still fails on is its CORPUS, not the bid store: "
        "`_t294_corpus` wires no trust snapshot and its store agents' offers carry no "
        "checkout_url, so since T-310 put the ranker on the served path every one of its "
        "candidates is excluded `blacklist_unreadable` + `off_domain_checkout` and `ranked` "
        "comes back []. The book holds only candidates the ranking ADMITTED, because recording "
        "the excluded ones was measured to let a blacklisted store be bought for a live code "
        "(409 -> 200 with PSX-…), and nothing on the accept path re-reads the trust snapshot. "
        "So the 200 this node demands is now reachable only through that hole. Closing it "
        "needs `_t294_corpus` to wire a trust snapshot and emit an on-domain checkout_url — an "
        "edit to this file, which the fixing lane was not scoped to make; remove this marker "
        "with that corpus change"
    ),
)
def test_t294_a_bid_the_exchange_just_returned_can_be_accepted() -> None:
    """A bid the exchange published in its own response must be one it can be asked to accept.

    ``POST /auctions`` calls ``solicit_bids`` (``auction/routes.py:538``), renders
    ``result.entries`` into the response through ``_entries_out`` (``:575``), ranks them
    (``:559``), stores the shortlist (``:569``) — and drops the ``BidEntry`` list.
    ``machine.close`` at ``:557`` only stamps ``closed_at``/``history``, and ``AuctionRecord``
    (``auction/state.py:100``) has no ``bids`` field at all. The accept route reads bids
    through ``app.state.auction_bids``, whose default is ``NoRecordedBids``
    (``accept/routes.py:110``), so ``accept()`` -> ``_find_bid`` -> ``_bids_of``
    (``accept/offer.py:209``) is handed an empty sequence and refuses everything.

    Measured at HEAD against a COMPLETE deployment — eligibility, solicitor, registered
    domains and ``CHECKOUT_MODE`` all wired, and only ``bids=`` left at its default::

        20 auctions, 55 non-fallback bids returned in `entries`
        20 accepts of a bid the exchange had just published -> 20 x
          409 {"accepted": false, "denial_reason":
               "unknown_bid: auction 'auction-…' carries no bid 'bid-store-37';
                there is nothing to accept"}

    and with the one-line recording fix applied, the same wiring, same corpus::

        POST /auctions/{id}/accept -> 200
          {"permalink_url": "https://store-1.example/cart/1:1?discount=PSX-6E3YCST1",
           "code": "PSX-6E3YCST1"}

    So the property has a real, observable positive form — a minted discount code — and that
    is what is asserted.

    Both halves are already built and simply not joined: ``InMemoryAuctionBids.record`` exists
    at ``accept/routes.py:136`` and ``configure_accept(..., bids=...)`` at ``:177``/``:211``.
    Note ``configure_auctions`` has no ``bids=`` parameter today, so the join is not a
    one-keyword change. The C18-accept lane documented the gap in its own module docstring
    (``accept/routes.py:34-44``) and reported it rather than fixing it, because
    ``auction/routes.py`` was out of its scope.

    **THE WRONG FIX, EXPLICITLY — and unlike the first draft of this gate, it is now actually
    REFUSED rather than merely deprecated in prose.** Do not rebuild bids from ``roster``.
    ``RosterEntry`` carries ``list_price`` and no ``store_domain``, so an accept built from a
    roster row would hand the buyer a discount computed against a price the store never
    offered, and would leave the registered-domain check with no domain to check — re-opening
    T-169 through the back door. Measured: that fix produces ``409 checkout_refused:
    OffDomainCheckout: the platform holds no registered domain for 'store-24'; the bid's claim
    '' is not evidence of one`` on 20 of 20 accepts, so it cannot reach the 200 below.

    **The assertion is HTTP 200 with a minted code — not "the reason is not ``unknown_bid``",
    and not an allowlist of denial codes.** Both weaker forms were defeated by an adversarial
    pass, and cheaply:

    * a negative on the string prefix is satisfied by ``auction_not_acceptable``, which
      ``accept_bid`` answers *before* the bid store is consulted at all
      (``accept/routes.py:322``), and by ``_denied()`` re-publishing any undeclared code as
      ``unspecified: <prose>``;
    * an allowlist of "codes reachable only after ``_find_bid`` returns" was defeated by a
      ONE-LINE relabel — ``DENIAL_UNKNOWN_BID = "unavailable"`` in ``accept/reasons.py``,
      nothing else touched, no bid recorded, all 20 accepts still refused — because only the
      code *word* was being graded.

    Demanding the 200 removes the string from the loop entirely: a discount code either exists
    or it does not, and no relabelling produces one. Each auction is still accepted exactly
    ONCE — a second accept would answer ``already_accepted`` for reasons unrelated to the bid
    store. :data:`T294_CODES_REACHED_ONLY_AFTER_THE_BID_IS_FOUND` is retained only to make the
    failure message say whether a refusal at least got past the lookup.
    """
    corpus = _t294_corpus()
    client = corpus["client"]
    auctions = corpus["auctions"]
    seed = corpus["seed"]

    pairs = [(auction["auction_id"], bid) for auction in auctions for bid in auction["bids"]]
    assert len(pairs) >= 40, (
        f"the shared corpus holds {len(pairs)} non-fallback bids (seed {seed}); this gate "
        "asserts nothing about a corpus that was not built, and a loop over an empty list is "
        "the way this gate would go quiet rather than red (see "
        "test_the_recorded_bid_corpus_is_armed)"
    )

    refusals: list[str] = []
    fabricated: list[str] = []
    minted = 0
    examined = 0
    for auction in auctions:
        if not auction["bids"]:
            continue
        # Exactly one accept per auction. A second would be refused `auction_not_acceptable`
        # or `already_accepted` for reasons that have nothing to do with the bid store.
        bid = auction["bids"][0]
        examined += 1

        # FIRST, a ref the exchange never published. A store with a real BOOK refuses it; a
        # store that FABRICATES a bid for whatever ref it is handed mints for it. That second
        # shape passed the earlier version of this gate — the unguessable token protects only
        # against an attacker who has to guess, and `accept_bid` is handed the ref — so the
        # question is asked directly. It runs before the real accept because a refusal leaves
        # the auction open, while an acceptance would close it.
        forged_ref = f"bid-{'f' * 8}{examined:04d}"
        forged = client.post(
            f"/auctions/{auction['auction_id']}/accept", json={"bid_ref": forged_ref}
        )
        forged_payload = forged.json()
        if forged.status_code == 200 and forged_payload.get("code"):
            fabricated.append(
                f"{forged_ref} (never published by this auction) -> 200 "
                f"{forged_payload.get('code')}"
            )

        answer = client.post(
            f"/auctions/{auction['auction_id']}/accept", json={"bid_ref": bid["bid_ref"]}
        )
        payload = answer.json()
        # The positive form: a 200 carrying a real permalink and a real single-use code.
        if answer.status_code == 200 and payload.get("code") and payload.get("permalink_url"):
            minted += 1
            continue
        reason = str(payload.get("denial_reason", "")) or answer.text
        code = reason.split(":", 1)[0].strip()
        past_lookup = code in T294_CODES_REACHED_ONLY_AFTER_THE_BID_IS_FOUND
        refusals.append(
            f"{bid['bid_ref']} -> {answer.status_code} {reason[:150]} "
            f"[{'past the bid lookup' if past_lookup else 'BEFORE the bid lookup'}]"
        )

    assert examined >= 15, (
        f"only {examined} auctions carried a bid to accept (seed {seed}); the sweep is unarmed"
    )

    assert not fabricated, (
        f"{len(fabricated)} auction(s) minted a discount code for a bid ref the exchange NEVER "
        f"PUBLISHED (seed {seed}). That is not a bid store, it is a rubber stamp: the accept "
        "path must be able to say no to a ref it has no record of, or 'the bid was recorded' "
        "and 'the exchange says yes to anything' are indistinguishable:\n  "
        + "\n  ".join(fabricated[:5])
    )

    assert not refusals, (
        f"{minted} of {examined} bids that the exchange itself returned in its own `entries` "
        f"were acceptable; {len(refusals)} were refused (seed {seed}). The deployment is fully "
        "wired except for the bid store, so a bid the auction published and still holds a "
        "record of would mint a discount code. The bracketed note says whether each refusal "
        "even got as far as the bid lookup:\n  " + "\n  ".join(refusals[:10])
    )


# =====================================================================================
# T-244 — the external bid door runs for nobody, and a frozen metric counts it as met
# =====================================================================================


def _bid_door_dispatch_probe() -> dict[str, Any]:
    """Build the real apps, POST at every door they serve, and report whether ``receive_bid`` RAN.

    **This replaced an AST-plus-import-closure conjunction that an adversarial pass defeated
    eleven ways, and the reason it failed is worth stating once: co-location is not execution.**
    That version asked three static questions — is ``store_agent.external.door`` in the built
    app's import closure, does some module in that closure contain a ``Call`` node named
    ``receive_bid``, does the app serve any route — and all three were satisfiable without a
    single request ever reaching the door. Measured, with ``door._receive_bid`` instrumented:

    * a mounted ``/healthz`` plus an unused ``_unused_probe`` in ``main.py`` — green, 0 entries;
    * a real ``POST /v1/bid-requests`` returning a canned ``{"accepted": true}`` beside an
      ``_unused_helper`` nobody calls — green, 0 entries, and that "door" admits every
      submission with no signature check at all;
    * a handler calling a LOCAL function it defines itself and names ``receive_bid`` — green, 0
      entries. It never imports ``door.py``; the closure conjunct passes because importing
      ``store_agent.external.routes`` executes the package ``__init__``, which already does
      ``from .door import receive_bid``, and ``_calls_named`` matches a NAME, not a binding.

    Worse, it was anti-correlated in both directions. The genuine wiring with
    ``include_in_schema=False`` was reported RED with a self-contradicting message ("the app
    serves NO routes at all (mounted routers: ['store_agent.external.routes'])"), because the
    served set was read from ``app.openapi()`` — a documentation artifact — while the real
    route table was collected and never asserted on.

    So the probe now DISPATCHES. It wraps ``door._receive_bid`` (the module global the total
    wrapper resolves at call time, so ``from .door import receive_bid`` cannot bypass the
    counter), builds each app, and POSTs a bid-shaped body at every POST route each app serves.
    "Reachable" then means what the ticket means: a request that arrives at a published door
    traverses the function. A dead helper cannot fake it and a local shadow cannot fake it.

    **Both services are asked, and that is deliberate.** The store agent's own contract says of
    ``POST /v1/bid-requests``: "SOLICITATION ONLY: exchange -> seller. This is not the inbound
    door. An external seller submits a bid to the exchange's ``POST
    /v1/auctions/{auction_id}/bids`` instead." Pinning the store-agent path would pin the door
    to a path the contract says is not the door, and an exchange-side wiring — the shape the
    contract actually describes — was falsely reported RED by the store-agent-only version.
    Either service reaching the door satisfies the property.
    """
    import os  # noqa: PLC0415 - kept out of this file's frozen import head
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    repo_root = Path(__file__).resolve().parents[3]
    code = r"""
import json, sys

entries = {"n": 0}
import store_agent.external.door as door

_real = door._receive_bid


def _counting(*args, **kwargs):
    entries["n"] += 1
    return _real(*args, **kwargs)


door._receive_bid = _counting

# A plausible signed external submission. It does NOT have to be VALID: `receive_bid` is
# documented "Never raises" and refuses hostile input by returning a receipt, so ENTERING it
# is the measurement. A handler that answers without entering it is the thing being detected.
BODIES = [
    {"payload": {"auction_id": "a-1", "store_id": "s-1", "signer_id": "s-1", "key_id": "k-1",
                 "issued_at": 0, "nonce": "n-1", "schema_version": "1.0.0",
                 "offer": {"product_ref": "p-1", "unit_price": 1.0, "total_price": 1.0}},
     "signature": "sig", "keyring": {"s-1": {"k-1": "secret"}}},
    {"auction_id": "a-1", "store_id": "s-1", "signature": "sig",
     "offer": {"product_ref": "p-1", "unit_price": 1.0, "total_price": 1.0}},
    {},
]

# Every POST path the app serves, walking NESTED routers.
#
# `app.routes` is not flat on this FastAPI: `include_router` leaves `_IncludedRouter`
# wrappers whose own `.routes` hold the real entries, and the wrappers carry no `path` or
# `methods` of their own. A non-recursive walk sees only /openapi.json, /docs and /redoc,
# which is exactly the empty POST set an earlier version of this probe measured.
#
# NB: no triple-quoted string may appear anywhere in this subprocess source. It is carried
# in a triple-quoted literal in the enclosing file, and the formatter normalises that
# literal's quote style, so an inner docstring silently terminates it and the rest of this
# code becomes module-level syntax in the TEST file. That is a real bug this file already hit.
def _post_paths(app):
    found = set()
    stack = list(getattr(app, "routes", []) or [])
    seen = 0
    while stack and seen < 5000:
        seen += 1
        node = stack.pop()
        stack.extend(getattr(node, "routes", []) or [])
        # `_IncludedRouter` is a dispatch shim with no `routes` of its own; the APIRouter that
        # actually holds the entries hangs off `original_router`. `app` covers sub-application
        # mounts. Following all three keeps this working whatever wrapper the version uses.
        for attr in ("original_router", "app"):
            nested = getattr(node, attr, None)
            if nested is not None and nested is not node:
                stack.append(nested)
        path = getattr(node, "path", None)
        methods = getattr(node, "methods", None) or set()
        if path and "POST" in methods:
            found.add(path)
    return sorted(found)


report = {}
for label, builder in (("store_agent", "store_agent.main"), ("exchange", "exchange.main")):
    before = entries["n"]
    try:
        import importlib

        module = importlib.import_module(builder)
        app = module.create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        posts = _post_paths(app)
        for path in posts:
            concrete = path
            for opening, closing in (("{", "}"),):
                while opening in concrete:
                    head, _, rest = concrete.partition(opening)
                    _var, _, tail = rest.partition(closing)
                    concrete = head + "x-1" + tail
            for body in BODIES:
                try:
                    client.post(concrete, json=body)
                except Exception:
                    pass
        report[label] = {
            "built": True,
            "posts": posts,
            "mounted": list(getattr(app.state, "mounted_routers", []) or []),
            "door_entries": entries["n"] - before,
        }
    except Exception as exc:
        report[label] = {"built": False, "error": f"{type(exc).__name__}: {exc}",
                         "posts": [], "mounted": [], "door_entries": entries["n"] - before}

report["modules"] = {name: getattr(m, "__file__", "") or "" for name, m in sys.modules.items()}
report["total_door_entries"] = entries["n"]
print(json.dumps(report))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), str(repo_root / ".pkgroot")])
    env["PROXYSHOP_WORKER"] = env.get("PROXYSHOP_WORKER", "0")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, (
        f"the bid-door dispatch probe would not run:\n{completed.stderr[-3000:]}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _store_agent_app_probe() -> dict[str, Any]:
    """What BUILDING the store agent's real app imports, AND what it ends up serving.

    A sibling of :func:`_exchange_app_import_closure` and a subprocess for the same reason —
    in-process, ``sys.modules`` already holds whatever the rest of the session imported, and a
    reachability question answered against a polluted module table answers itself in the
    affirmative every time. It differs in two deliberate ways.

    First, it returns the WHOLE module table, not just the ``store_agent.*`` slice, because a
    production call site may live in any package and the question is whether the serving
    process reaches it.

    Second, it reports ``paths`` and ``mounted`` as well, and that is not decoration. An
    adversarial pass defanged the earlier module-only version with a file containing nothing
    but ``router = None`` beside a dead function naming ``receive_bid``: ``create_app()``'s
    glob IMPORTS every ``<feature>/routes.py`` it finds and only then checks for a ``router``
    attribute, so a module that mounts nothing still lands in the import closure. Being
    imported is not being served; the gate now asks for both.
    """
    import os  # noqa: PLC0415 - kept out of this file's frozen import head
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    repo_root = Path(__file__).resolve().parents[3]
    code = (
        "import json, sys\n"
        "import store_agent.main\n"
        "app = store_agent.main.create_app()\n"
        "print(json.dumps({\n"
        "    'modules': {name: getattr(module, '__file__', '') or ''\n"
        "                for name, module in sys.modules.items()},\n"
        "    'paths': sorted(app.openapi().get('paths', {})),\n"
        "    'mounted': list(getattr(app.state, 'mounted_routers', []) or []),\n"
        "}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), str(repo_root / ".pkgroot")])
    env["PROXYSHOP_WORKER"] = env.get("PROXYSHOP_WORKER", "0")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"the store agent app would not build:\n{completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _t244_production_call_sites(symbol: str) -> list[tuple[str, Path, int]]:
    """Real AST call sites of ``symbol`` in non-test source, as (module-ish name, path, line).

    Uses the file's existing :func:`_calls_named` so a docstring that spells the call out as an
    example — ``external/__init__.py`` has several — cannot be counted; the parser never
    produces a ``Call`` node for a string constant.
    """
    repo_root = Path(__file__).resolve().parents[3]
    roots = [repo_root / "apps", repo_root / "packages", repo_root / "services"]
    found: list[tuple[str, Path, int]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            parts = set(path.parts)
            if "tests" in parts or "test" in parts or path.name.startswith("test_"):
                continue
            if ".venv" in parts or "node_modules" in parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (
                OSError,
                SyntaxError,
            ):  # pragma: no cover - a file we cannot parse is not a call
                continue
            for lineno in _calls_named(tree, symbol):
                found.append((path.stem, path, lineno))
    return found


def test_the_store_agent_import_closure_probe_is_armed() -> None:
    """NOT xfail, for the reason its exchange sibling spells out at length.

    Under ``xfail(strict=True)`` a probe that had stopped working — a subprocess that will not
    build the app, a JSON decode error, a timeout — is reported ``xfailed``, which is green,
    and under ``--runxfail`` every one of those dead states reads as "still broken", so a lane
    repairing the ticket would churn against a probe that could never go green. Both failure
    modes are removed by asserting the probe's own health here, in its own name.

    The third check is the one that catches a silently-narrowed sweep: the AST scanner must
    find call sites of a symbol that certainly HAS them. ``edit_envelope`` is the control, and
    it is a deliberate choice — see the gate's docstring for why this ticket's own claim about
    it turned out to be wrong.
    """
    repo_root = Path(__file__).resolve().parents[3]
    probe = _bid_door_dispatch_probe()
    modules = probe["modules"]

    assert modules, "the dispatch probe imported no module at all; the probe is wrong"
    assert "store_agent.external.door" in modules, (
        "the probe never imported store_agent.external.door, so its instrumentation of "
        f"`_receive_bid` was never installed and a zero entry count means nothing "
        f"(it saw {len(modules)} modules)"
    )
    for label in ("store_agent", "exchange"):
        assert probe[label]["built"], (
            f"the {label} app would not build inside the probe, so its door-entry count of "
            f"{probe[label]['door_entries']} is not evidence of anything: "
            f"{probe[label].get('error')}"
        )
    assert probe["exchange"]["posts"], (
        "the exchange app served no POST route inside the probe; it serves POST /auctions and "
        "POST /auctions/{auction_id}/accept, so the probe is dispatching against the wrong "
        f"object (it saw mounted={probe['exchange']['mounted']})"
    )
    for name, filename in sorted(modules.items()):
        if not filename or not name.startswith("store_agent"):
            continue
        resolved = Path(filename).resolve()
        assert repo_root in resolved.parents, (
            f"{name} resolved to {resolved}, outside the tree under test ({repo_root}) — the "
            "venv's _proxyshop.pth puts a checkout root on sys.path for every process, so a "
            "probe that trusts the resolution it happens to get can measure another checkout"
        )

    control = _t244_production_call_sites("edit_envelope")
    assert control, (
        "the AST scanner found no production call site of `edit_envelope`, which certainly "
        "has one at apps/merchant/svc/src/envelope/store.py:130 — the scanner is broken or "
        "its roots glob matched nothing, and every 'zero callers' verdict it returns would be "
        "vacuous"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-244: `receive_bid` — the Tier-2 door with six gates that the frozen E4 suite "
        "exercises directly — has ZERO production call sites, and the module defining it is "
        "not in the import closure of the store agent's own served app (which builds to "
        "store_agent + store_agent.main and mounts no routes at all). So "
        "e4_store_agent_passing counts a signed-external-bid capability that no running "
        "process performs; remove this marker with the fix"
    ),
)
def test_t244_the_external_bid_door_is_reachable_from_a_served_process() -> None:
    """Something a frozen metric counts as delivered must be reachable by a running process.

    ``packages/store-agent/src/external/door.py`` implements ``receive_bid`` at line 361 — six
    gates: shape, envelope completeness, authentication with no key trial, freshness plus the
    auction deadline, the shared Tier-2 boundary, and nonce replay. Measured at HEAD,
    ``git grep -n receive_bid -- '*.py'`` returns 103 line-hits across nine files, and after
    classification::

        test files                             87 hits (all executable references)
        door.py                                16 hits: 1 def, 1 __all__, 1 call to the
                                                        PRIVATE `_receive_bid`, 13 prose
        external/__init__.py                    3 hits: `from .door import receive_bid`
                                                        (a re-export, not a call), __all__,
                                                        one docstring line
        nonces.py / contracts/src/signing.py    3 hits: comments and a docstring

    Zero production call sites. And no app or service imports the package at all — ``git grep
    store_agent`` over ``apps/*/src``, ``apps/*/svc/src`` and ``services/*/src`` returns only
    comments. Measured from the other direction, which is what this test asserts::

        >>> store_agent.main.create_app().state.mounted_routers
        []
        >>> sorted(m for m in sys.modules if m.startswith('store_agent'))
        ['store_agent', 'store_agent.main']

    so ``store_agent.external.door`` is not merely uncalled, it is never even imported by the
    process that would serve a request.

    **Why the property is a CONJUNCTION rather than two assertions side by side.** Asked
    separately, "is a route served?" and "does some file call ``receive_bid``?" are both cheap
    to satisfy without wiring anything: add a handler that validates inline, and drop one call
    into a module the app never imports. Both go green; no request reaches the door. The
    reachability measurement is what joins them — the module holding the call site must itself
    be in the import closure of the built app — and that is a property a dead module cannot
    fake. It is also what distinguishes this from T-309, whose gate in
    ``packages/store-agent/tests/test_repro_open_tickets.py:195`` asks only whether the served
    route table matches the published contract; its own docstring draws the line: "This is
    distinct from ``receive_bid`` being unwired in production: the function is not merely
    unwired, there is no route that could wire it."

    **The ticket's second half is FALSE, and this gate deliberately does not assert it.**
    T-244 claims merchant ``edit`` is the same shape — "TESTS ONLY, 14 callers, 0 product" —
    and locates ``edit_envelope`` in ``apps/merchant/svc/src/envelope/store.py``. Measured:
    ``edit_envelope`` is DEFINED at ``apps/merchant/svc/src/envelope/versions.py:36``;
    ``store.py:22`` imports it and ``store.py:130`` calls it in production
    (``return self.record(edit_envelope(head, changes))``) from ``EnvelopeVersions.put``, which
    is served over HTTP at ``onboarding/routes.py:109`` (``PUT /stores/{store_id}/envelope``,
    reaching ``put`` at ``:139``). What is true is narrower and harmless: the NAME ``edit`` has
    no non-test callers, because ``onboarding/flow.py:160`` is ``edit = edit_envelope`` — an
    alias whose own comment says the bound object "IS the one ``EnvelopeVersions`` calls" and
    is "byte for byte the function ``PUT /stores/{id}/envelope`` runs". A dead *name* in front
    of a live *function* is not the ``receive_bid`` situation, and a gate that asserted both
    halves identically would be red for a defect that does not exist. ``edit_envelope`` is used
    instead as the arming control for the AST scanner, where a symbol with a known production
    caller is exactly what is needed.
    """
    probe = _bid_door_dispatch_probe()
    entries = int(probe["total_door_entries"])
    call_sites = _t244_production_call_sites("receive_bid")

    detail = "; ".join(
        f"{label}: built={probe[label]['built']}, POST routes={probe[label]['posts'] or 'none'}, "
        f"mounted={probe[label]['mounted'] or 'none'}, "
        f"door entries={probe[label]['door_entries']}"
        + (f", error={probe[label]['error']}" if not probe[label]["built"] else "")
        for label in ("store_agent", "exchange")
    )

    assert entries > 0, (
        "no request that either service serves reaches the Tier-2 external bid door. Every "
        "POST route on the built store-agent app and the built exchange app was dispatched a "
        "bid-shaped body, and `store_agent.external.door._receive_bid` — the function behind "
        "the six gates the frozen E4 suite exercises directly — was entered zero times. The "
        f"AST scan separately finds {len(call_sites)} production call site(s) of receive_bid "
        f"{[f'{p}:{n}' for _s, p, n in call_sites][:5]}, which is context, not the assertion: "
        "a call site proves someone wrote the name, and this gate is about whether a request "
        f"arrives. Measured: {detail}. So e4_store_agent_passing counts a signed-external-bid "
        "capability that no running process performs."
    )
