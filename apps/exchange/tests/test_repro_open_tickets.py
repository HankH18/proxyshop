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

    Neo4j is NOT in that path, and it was worth measuring rather than assuming: ``filters``
    imports ``retrieval.criteria`` only, never ``retrieval.sources``/``service``, and
    ``criteria`` reaches ``ingest.graph.model.slug`` and ``AttributeFilter`` — pure
    in-process string folding and a frozen dataclass. Reproduced with a meta-path finder that
    raises on any ``neo4j`` import and ``socket`` disabled: ``rank()`` ran to completion and
    applied its R12/R19/C10 exclusions. A Neo4j session, the driver pin and ``NEO4J_*`` env
    are what ``exchange.retrieval`` needs (T-260), not what this gate is about.
    """
    modules = _exchange_app_import_closure()
    ranking = sorted(name for name in modules if name.split(".")[:2] == ["exchange", "ranking"])
    assert ranking, (
        "building the exchange app imports no exchange.ranking module, so the published "
        "ranking — rank(), the hard-constraint filters and the shortlist builder — is on no "
        f"served path; the app's exchange import closure is {sorted(modules)}"
    )
