"""Reproductions for the three exchange LEDGER tickets that still carry a PLACEHOLDER gate.

``tickets.json`` records ``verify: "false  # NO GATE YET"`` for T-150, T-282 and T-283, and a
ticket cannot be dispatched until a gate is proven red on today's code. This file is that
proof for the three of them. It is a sibling of ``test_repro_open_tickets.py`` rather than an
addition to it: that file is the T-158/T-169/T-170/T-204/T-235/T-250/T-270/T-293/T-294/T-310/
T-312 lane's, and three more gates hung off the end of a 3 000-line file that other lanes
grade against is how a merge conflict becomes a lost gate.

The mechanism, in both directions:

* a normal run reports ``xfailed`` and exits 0, so ``pytest apps/exchange -q`` stays green;
* the ticket's gate runs ``--runxfail -k <selector>`` and gets a real ``1 failed``;
* ``strict=True`` turns the eventual repair into an XPASS *failure*, so whoever fixes the
  defect must delete the marker. The gate cleans itself up.

Three gates, three selectors::

    -k t150    test_t150_a_served_auction_puts_its_transitions_in_the_trust_ledger
    -k t282    test_t282_the_auction_package_re_exports_every_name_its_ledger_module_publishes
    -k t283    test_t283_a_malformed_audit_record_does_not_cost_a_buyer_a_minted_code

...and three ARMING tests that are deliberately **not** xfail. Under ``xfail(strict=True)`` any
exception in a test body is reported ``xfailed`` — which is green — so a probe that has
stopped working is indistinguishable from the defect it is supposed to detect, and under the
ticket's own ``--runxfail`` gate a dead probe reads as "still broken" forever. Everything that
could make a gate below measure NOTHING and report success is asserted in an armer, in its own
name, where it fails ``make verify`` loudly. None of the armer names contains a ticket number,
so ``-k tNNN`` selects exactly one node and a per-gate run is exactly ``1 failed``.

**Two of the three tickets reproduce exactly as written; T-150 does not.** Its "nothing in the
repository writes an event into it" is FALSE at HEAD — ``services/sim`` and ``e2e/support``
both feed the trust writer — and ``tickets.json`` records the ticket itself ``closed`` /
REFUTED while ``.swarm-loop/backlog.md`` still lists it as open. Its gate is written to the
narrower property that does hold, and the banner above that section carries the correction and
the reasons to keep or delete it. The other two sections do not depend on it.

Nothing here touches product source. A lane that repairs the defect it was asked to reproduce
destroys the gate that would have graded the repair.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

#: The worktree these gates are measuring, reached from this file rather than from a hard-coded
#: string. Every probe below asserts that the modules it imported resolve INSIDE it: this venv's
#: ``site-packages/_proxyshop.pth`` puts a hard-coded checkout root and its ``.pkgroot`` on
#: ``sys.path`` for every process that uses it, and that has already produced one false green in
#: this repo. A probe that trusts the resolution it happens to get can measure a different tree.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``getattr`` default. ``None`` will not do: a package that re-exports a name bound to ``None``
#: is a different defect from one that does not re-export it at all, and the T-282 probe has to
#: be able to say which.
_UNBOUND = object()


def _resolves_inside_the_tree(*modules: Any) -> list[str]:
    """Every module among ``modules`` whose file is not under :data:`REPO_ROOT`."""
    stray: list[str] = []
    for module in modules:
        filename = getattr(module, "__file__", "") or ""
        if not filename:
            stray.append(f"{module.__name__}: has no __file__ at all")
            continue
        resolved = Path(filename).resolve()
        if REPO_ROOT not in resolved.parents:
            stray.append(f"{module.__name__}: {resolved}")
    return stray


# =====================================================================================
# T-282 — the auction package does not re-export what its ledger module publishes
# =====================================================================================


def _re_export_gaps(package: Any, module: Any) -> list[str]:
    """Every name ``module.__all__`` publishes that ``package`` does not actually re-export.

    Three distinguishable failures, and they are reported separately because they are three
    different one-line fixes:

    * the name is not bound on the package at all — ``from <package> import <name>`` raises
      ``ImportError``, which is T-282's measured symptom;
    * the name is bound to a *different object* than the module's — a package that rebinds a
      re-exported exception class turns ``except <package>.MalformedLedgerPayload`` into a
      clause that never catches what ``build_published_event`` raises, which is worse than the
      ImportError because it fails silently;
    * the name is bound but missing from the package's own ``__all__`` — importable, but absent
      from the surface the package publishes, so ``from <package> import *`` and every tool that
      reads ``__all__`` still cannot see it.

    ``__all__`` is read off the MODULE rather than off a hand-written list, so a name added to
    ``ledger.py``'s ``__all__`` tomorrow is graded the day it lands. That is the whole reason
    T-282 exists: the block in ``auction/__init__.py`` was written once and never re-derived.
    """
    gaps: list[str] = []
    published = tuple(getattr(module, "__all__", ()))
    package_all = tuple(getattr(package, "__all__", ()))
    for name in published:
        bound = getattr(package, name, _UNBOUND)
        if bound is _UNBOUND:
            gaps.append(f"{name}: not bound on the package at all (ImportError)")
        elif bound is not getattr(module, name, _UNBOUND):
            gaps.append(f"{name}: the package binds a DIFFERENT object than the module does")
        elif name not in package_all:
            gaps.append(f"{name}: importable, but missing from the package's own __all__")
    return gaps


def test_the_auction_re_export_probe_is_armed() -> None:
    """NOT xfail, and the separation is the whole reason it exists.

    Four ways :func:`_re_export_gaps` could return ``[]`` while measuring nothing, all closed
    here:

    * ``ledger.__all__`` empties or shrinks, so the loop iterates zero (or too few) names and
      ``gaps == []`` means "nothing was checked" rather than "nothing is missing";
    * either module resolves outside this worktree, so the answer describes another checkout;
    * the detector stops detecting — a rewrite that returns ``[]`` unconditionally passes the
      gate below forever. Three planted positives, one per failure mode, close that;
    * the detector starts matching everything, so the gate would be red for a reason that is
      not the ticket. A clean synthetic control and one live name that IS correctly re-exported
      (``UnknownLedgerEventKind``, whose resolution is half of the ticket's own MEASURED line)
      close that.
    """
    import exchange.auction as package  # noqa: PLC0415 - kept out of this file's frozen import head
    import exchange.auction.ledger as ledger  # noqa: PLC0415

    stray = _resolves_inside_the_tree(package, ledger)
    assert not stray, (
        f"these modules resolved outside the tree under test ({REPO_ROOT}): {stray} — that is "
        "the _proxyshop.pth leak, not a measurement of this branch"
    )

    published = tuple(getattr(ledger, "__all__", ()))
    assert len(published) >= 8, (
        f"exchange.auction.ledger.__all__ publishes {len(published)} name(s) {published}; it "
        "carried eight when this gate was written, and a shrunken __all__ makes the sweep below "
        "measure less than it was built to measure — deleting a published name is not a fix"
    )

    # A CLEAN synthetic control: a package that re-exports everything its module publishes must
    # produce no gaps. Without this, a detector that returns a non-empty list unconditionally
    # would keep the gate red after a real repair.
    clean_module = types.SimpleNamespace(__all__=["Alpha", "beta"], Alpha=object, beta=len)
    clean_package = types.SimpleNamespace(
        __all__=["Alpha", "beta"], Alpha=clean_module.Alpha, beta=clean_module.beta
    )
    assert _re_export_gaps(clean_package, clean_module) == [], (
        "the probe reports gaps against a package that re-exports everything; it would keep "
        "T-282's gate red through any repair"
    )

    # THREE PLANTED POSITIVES, one per failure mode the gate is supposed to catch.
    absent = types.SimpleNamespace(__all__=["Alpha"], Alpha=clean_module.Alpha)
    assert len(_re_export_gaps(absent, clean_module)) == 1, (
        "the probe did not notice a name its module publishes and its package does not bind — "
        "which is exactly T-282's shape"
    )

    rebound = types.SimpleNamespace(
        __all__=["Alpha", "beta"], Alpha=clean_module.Alpha, beta=object()
    )
    assert len(_re_export_gaps(rebound, clean_module)) == 1, (
        "the probe did not notice a package binding a DIFFERENT object under a re-exported "
        "name — the silent half of this defect, where `except pkg.Error` stops catching"
    )

    undeclared = types.SimpleNamespace(
        __all__=["Alpha"], Alpha=clean_module.Alpha, beta=clean_module.beta
    )
    assert len(_re_export_gaps(undeclared, clean_module)) == 1, (
        "the probe did not notice a name that is importable but absent from the package's own "
        "__all__ — half of what T-282 reports (`the package __all__ at :64-100 omits both`)"
    )

    # ...and one LIVE negative control. The ticket's own measurement is a PAIR — the sibling
    # defined in the same module and listed in the same ledger __all__ resolves — and a probe
    # that reported every name missing (because the package failed to import, say) would satisfy
    # the gate's assertion for a reason that has nothing to do with the re-export block.
    gaps = _re_export_gaps(package, ledger)
    assert not [gap for gap in gaps if gap.startswith("UnknownLedgerEventKind")], (
        "the probe reports UnknownLedgerEventKind as un-re-exported, but the ticket MEASURED it "
        f"resolving; the probe is reporting the whole package missing. Gaps: {gaps}"
    )


def test_t282_the_auction_package_re_exports_every_name_its_ledger_module_publishes() -> None:
    """The package's import surface must carry what the module it wraps publishes.

    Measured at HEAD, from the worktree root with ``PROXYSHOP_WORKER=7``::

        from exchange.auction import MalformedLedgerPayload
          -> ImportError: cannot import name 'MalformedLedgerPayload' from 'exchange.auction'
        from exchange.auction import build_published_event
          -> ImportError: cannot import name 'build_published_event' from 'exchange.auction'
        from exchange.auction import published_body
          -> ImportError: cannot import name 'published_body' from 'exchange.auction'
        from exchange.auction import UnknownLedgerEventKind
          -> OK  <class 'exchange.auction.ledger.UnknownLedgerEventKind'>

        exchange.auction.ledger.__all__  = ['InMemoryLedgerSink', 'LedgerRecorder',
            'LedgerSink', 'MalformedLedgerPayload', 'UnknownLedgerEventKind', 'build_event',
            'build_published_event', 'published_body']
        missing from exchange.auction.__all__ = ['MalformedLedgerPayload',
            'build_published_event', 'published_body']

    **THREE names, not the two the ticket lists.** ``published_body`` — ``ledger.py:63``, the
    function that answers "what keys does this kind's body carry" and the one a caller needs to
    build a body that will not be refused — is omitted as well and is named nowhere in T-282.
    Asserting the *property* rather than the ticket's list is what caught it, and is why this
    gate reads ``ledger.__all__`` instead of a hand-written triple: the next name added to
    ``ledger.py`` is graded the day it lands rather than the day somebody notices.

    Why it matters beyond tidiness: ``MalformedLedgerPayload`` is the exception
    ``build_published_event`` raises, so the only way to write ``except MalformedLedgerPayload``
    — which is precisely what T-283's repair at the consumers has to do — is to reach past the
    package into ``.ledger``, the import surface the package ``__init__`` exists to prevent.
    T-283's fix therefore cannot be written cleanly until this one lands. The in-repo convention
    is the other way round: the merchant's peer package re-exports its identically-named class
    at ``apps/merchant/svc/src/codes/__init__.py``.

    The fix is two edits in one file: add the three names to the ``from .ledger import (...)``
    block and to the package ``__all__``.
    """
    import exchange.auction as package  # noqa: PLC0415 - kept out of this file's frozen import head
    import exchange.auction.ledger as ledger  # noqa: PLC0415

    gaps = _re_export_gaps(package, ledger)
    assert not gaps, (
        "exchange.auction does not re-export everything exchange.auction.ledger publishes in "
        f"its own __all__ {tuple(getattr(ledger, '__all__', ()))}:\n  " + "\n  ".join(gaps)
    )


# =====================================================================================
# T-283 — a validating raise inside the region whose contract is "never fail the auction"
# =====================================================================================

#: The store, host, clock and offer every T-283 probe runs on. One legal, ordinary, mintable
#: bid: the point of this ticket is that the defect costs a buyer a code on the SUCCESS path,
#: so every input here has to be one the port is happy with.
T283_STORE = "store-t283"
T283_DOMAIN = "store-t283.example.com"
T283_NOW = 1_700_000_000.0
T283_OFFER: dict[str, Any] = {
    "product_ref": "prod-1",
    "unit_price": 80.0,
    "total_price": 80.0,
    "discount": 0.2,
    "quantity": 1,
}

#: The key the contract is made to publish and the exchange's producers do not write. The name
#: is arbitrary and deliberately not one of today's keys — the injection must ADD a requirement,
#: never redefine an existing one, or the probe would be measuring a different event shape.
T283_UNWRITTEN_KEY = "settlement_ref"


@contextmanager
def _contract_publishes_one_more_key(kind: str, key: str) -> Iterator[tuple[str, ...]]:
    """Make ``contracts.ledger`` publish one extra key for ``kind``, then put it back exactly.

    **This is the injection, and it is deliberately on the CONTRACT side rather than on the
    producer's.** T-283 is explicit that the defect is latent — "both call sites currently pass
    every published key ... It is one dropped key away" — so a gate for it has to create the
    divergence somehow, and there are only two ends to create it from. Editing the producer
    would mean overriding ``CheckoutProvider._events``, a method the port docstring says the
    port performs "identical for every provider"; a test that reaches in and rewrites the thing
    under test is grading its own edit.

    Widening the contract is the same divergence from the other end, it is the direction the
    repo has actually moved in (``LEDGER_PAYLOAD_SHAPES`` is what T-235 was measured against,
    and ``contracts/src/ledger.py`` calls itself "a description a producer is checked against"),
    and it touches nothing a fixing lane owns. One key is added; the tuple is otherwise
    untouched, and the arming test asserts both the widening and the restoration.

    ``validate_ledger_payload`` reads ``LEDGER_PAYLOAD_SHAPES`` as a module global at call time,
    so rebinding the name on ``contracts.ledger`` is enough — the function the exchange imported
    by value still closes over this module's ``__dict__``. ``exchange.auction.ledger`` holds its
    own by-value reference, used only by :func:`published_body` to FORMAT the refusal message,
    so the message quotes the un-widened body. That is cosmetic and is left alone on purpose:
    patching a second module would widen the blast radius of the injection for a prettier
    string.
    """
    import contracts.ledger as contract  # noqa: PLC0415 - kept out of this file's frozen import head

    original = contract.LEDGER_PAYLOAD_SHAPES
    widened = dict(original)
    widened[kind] = (*tuple(original[kind]), key)
    contract.LEDGER_PAYLOAD_SHAPES = widened
    try:
        yield tuple(widened[kind])
    finally:
        contract.LEDGER_PAYLOAD_SHAPES = original


def _t283_request() -> Any:
    """One ordinary, mintable checkout request against a PLATFORM-registered domain."""
    from exchange.checkout import CheckoutRequest, StaticRegisteredDomains  # noqa: PLC0415

    return CheckoutRequest(
        auction_id="auction-t283",
        bid_ref="bid-t283",
        store_id=T283_STORE,
        store_domain=T283_DOMAIN,
        offer=dict(T283_OFFER),
        mode="redirect",
        now=T283_NOW,
        registered_domains=StaticRegisteredDomains({T283_STORE: T283_DOMAIN}),
    )


def _t283_auction() -> dict[str, Any]:
    """A FRESH auction record each call — ``accept()`` stamps the one it is given."""
    return {
        "auction_id": "auction-t283",
        "now": T283_NOW,
        "bids": [
            {
                "bid_id": "bid-t283",
                "bid_ref": "bid-t283",
                "store_id": T283_STORE,
                "store_domain": T283_DOMAIN,
                "offer": dict(T283_OFFER),
            }
        ],
    }


def _t283_accept(auction: dict[str, Any]) -> Any:
    """``accept()`` with the platform registry wired and no durable claim table.

    ``claims=None`` is the explicit "no table" spelling documented on ``accept()``. It is passed
    rather than omitted so this probe cannot pick up whatever a sibling test wired process-wide
    through ``use_acceptance_claims`` and start refusing ``already_accepted`` for a reason that
    is not this ticket.
    """
    from exchange.accept import accept  # noqa: PLC0415 - kept out of this file's frozen import head
    from exchange.checkout import StaticRegisteredDomains  # noqa: PLC0415

    return accept(
        auction,
        "bid-t283",
        None,
        "redirect",
        registered_domains=StaticRegisteredDomains({T283_STORE: T283_DOMAIN}),
        claims=None,
    )


def test_the_malformed_audit_record_probe_is_armed() -> None:
    """NOT xfail. Six ways the T-283 gate could pass while measuring nothing, all closed here.

    * **The probe never mints.** Every assertion in the gate is about what happens to a
      SUCCESSFULLY MINTED code. A request that had drifted out of the port's contract — an
      unregistered domain, an unmintable offer — would be refused before a code existed, and the
      gate would then be grading a refusal that has nothing to do with the audit record. The
      clean control demands a real ``CheckoutResult`` carrying the C11 trio.
    * **The injection does not reach the raise.** ``build_published_event`` only refuses a body
      that is missing a published key; if the widening stopped landing, the gate would report
      the defect repaired. The planted positive calls it with the exact body the port writes.
    * **The injection leaks.** ``LEDGER_PAYLOAD_SHAPES`` is process-wide, and a widening that
      escaped this file would make unrelated suites red for a reason nobody could find. Both the
      widened tuple and the restored one are asserted.
    * **The modules under test are somebody else's.** ``_proxyshop.pth``, as everywhere here.
    * **The exception the gate is about becomes unreachable.** ``MalformedLedgerPayload`` is
      imported from ``exchange.auction.ledger`` rather than from ``exchange.auction``, because
      the package does not re-export it — that is T-282, and this line is the reason these two
      tickets are gated in one file.
    * **The repair goes the way the ticket forbids.** T-283 says the fix is at the CONSUMERS,
      "not by weakening the producing-boundary check T-235 was closed with". A lane that made
      ``build_published_event`` stop raising would turn the gate green and silently re-open
      T-235 — one kind with two bodies, the success path writing ``{checkout_token,
      discount_code}`` under ``code_created`` while the contract publishes ``(code,
      permalink_url, expires_at)``. The last assertion here is that tripwire: it fails
      ``make verify`` in its own name, and it survives the deletion of the xfail marker.
    """
    import contracts.ledger as contract  # noqa: PLC0415 - kept out of this file's frozen import head
    import exchange.accept.offer as offer_module  # noqa: PLC0415
    import exchange.checkout.provider as provider_module  # noqa: PLC0415
    from exchange.auction.ledger import (  # noqa: PLC0415
        MalformedLedgerPayload,
        build_published_event,
    )
    from exchange.checkout import SimulatedRedirectProvider  # noqa: PLC0415

    stray = _resolves_inside_the_tree(contract, offer_module, provider_module)
    assert not stray, (
        f"these modules resolved outside the tree under test ({REPO_ROOT}): {stray} — that is "
        "the _proxyshop.pth leak, not a measurement of this branch"
    )

    published = tuple(contract.LEDGER_PAYLOAD_SHAPES["code_created"])
    assert published == ("code", "permalink_url", "expires_at"), (
        f"contracts publishes {published} for 'code_created'; this file's injection adds ONE key "
        "to that tuple and the whole probe is built on knowing what it started as"
    )

    # CLEAN CONTROL 1 — the success path, uninjected, really mints and really files the trio.
    provider = SimulatedRedirectProvider()
    result = provider.checkout(_t283_request())
    assert result.code, "the uninjected checkout minted no code; the probe is not measuring one"
    assert result.kinds == ["accepted", "code_created", "checkout_redirect"], (
        f"the uninjected checkout filed {result.kinds}, not the C11 trio — the probe is no "
        "longer driving the path whose audit record this ticket is about"
    )

    # CLEAN CONTROL 2 — the refusal path's entry point, uninjected, accepts.
    accepted = _t283_accept(_t283_auction())
    assert accepted.accepted, (
        f"the uninjected accept() refused with {accepted.denial_reason!r}; the probe's bid is no "
        "longer one the exchange will take, so the gate below would grade an ordinary refusal"
    )

    # PLANTED POSITIVE — the injection reaches the raise, on the exact body the port writes.
    with _contract_publishes_one_more_key("code_created", T283_UNWRITTEN_KEY) as widened:
        assert widened == (*published, T283_UNWRITTEN_KEY), (
            f"the injection produced {widened}; it is supposed to add exactly one key"
        )
        assert tuple(contract.LEDGER_PAYLOAD_SHAPES["code_created"]) == widened, (
            "the injection did not land on contracts.ledger, so nothing below is measuring a "
            "malformed body"
        )
        with pytest.raises(MalformedLedgerPayload):
            build_published_event(
                "code_created",
                auction_id="auction-t283",
                store_id=T283_STORE,
                payload={
                    "code": result.code,
                    "permalink_url": result.permalink_url,
                    "expires_at": result.expires_at,
                    "checkout_token": result.checkout_token,
                    "discount_code": result.code,
                },
            )

    # ...and the injection is GONE. A widening that escaped this file would make unrelated
    # suites red for a reason nobody could trace back here.
    assert tuple(contract.LEDGER_PAYLOAD_SHAPES["code_created"]) == published, (
        "the contract widening outlived its context manager; every later test in this process "
        "is now grading a payload shape this file invented"
    )

    # THE T-235 TRIPWIRE, and it is the assertion most worth reading twice. It is NOT a
    # restatement of the planted positive: that one runs under the injection and proves the
    # probe works, this one runs against the REAL contract and proves the producing-boundary
    # check T-235 closed is still there. T-283's repair is at the consumers; a lane that
    # instead deleted the raise would make the gate below XPASS *and* silently restore the
    # one-kind-two-bodies defect. This armer goes red for that, in make verify, forever.
    with pytest.raises(MalformedLedgerPayload):
        build_published_event(
            "code_created",
            auction_id="auction-t283",
            store_id=T283_STORE,
            payload={"checkout_token": "tok", "discount_code": result.code},
        )


def test_t283_a_malformed_audit_record_does_not_cost_a_buyer_a_minted_code() -> None:
    """A defective AUDIT RECORD must not decide the auction. Two consumers, one property.

    **CLOSED — the ``xfail(strict=True)`` marker was deleted with the repair, which is what
    the marker was for.** What it said, kept because it is the measurement this node exists to
    hold and a gate whose reproduction is gone cannot be re-graded: ``build_published_event``
    (``auction/ledger.py``) was called from two regions that cannot absorb a raise —
    ``checkout/provider.py``'s ``self._events(...)`` inside the post-mint ``try`` whose
    ``except Exception`` raises ``OrphanedCheckoutCode``, and ``accept/offer.py``'s
    ``_orphan_record`` / ``policy_event``, which run inside ``_refused``, which ``accept()``
    invokes from an ``except`` block with no outer try. Measured with ``contracts`` publishing
    one key the producers do not write: a successfully minted live code became an
    ``OrphanedCheckoutCode`` refusal, and ``accept()`` let ``MalformedLedgerPayload`` escape
    uncaught.

    The repair is at the CONSUMERS, as the ticket required: ``build_published_event`` still
    raises on a body that is not the published one — ``test_the_malformed_audit_record_probe_
    is_armed``'s last assertion is the tripwire that keeps it raising — and each producer now
    drops the one event it could not build, records it through
    ``auction.ledger.record_audit_anomaly``, and hands back the outcome the buyer earned.

    **This node no longer covers the accept half on its own**, and that is why
    ``test_a_malformed_audit_record_leaves_a_refused_accept_a_refusal`` exists below: with the
    checkout half repaired, ``provider.checkout`` returns a result here, ``accept()`` succeeds,
    and nothing in this node reaches ``_refused`` any more. That node reaches it through an
    ordinary post-mint domain refusal instead, which does not depend on this defect at all.

    ``ledger.py``'s own module docstring states the contract this violates, in bold, at line 19:
    "**A sink that raises does not take the auction down.** Losing an audit record is bad;
    failing a live auction because the audit sink hiccuped is worse". ``LedgerRecorder.record``
    honours it — it swallows sink failures into ``failures``. ``build_published_event`` does
    not: it raises ``MalformedLedgerPayload`` from inside the two regions that are least able to
    absorb it, and both were measured.

    Measured at HEAD, with ``contracts.ledger`` publishing one key for ``code_created`` that
    neither producer writes — the divergence the ticket says the tree is "one dropped key away"
    from, created from the contract's end so that nothing under test is edited::

        (a) SUCCESS PATH   SimulatedRedirectProvider().checkout(request)
            uninjected  -> CheckoutResult, code PSX-7Q…, kinds
                           ['accepted', 'code_created', 'checkout_redirect']
            injected    -> OrphanedCheckoutCode:
                           "MalformedLedgerPayload: refusing to emit a 'code_created' event
                            whose body is not the published one … — raised AFTER
                            'simulated-redirect' minted code:1ef0d265c7e7 for store
                            'store-t283'; the code is live and must be recorded and revoked"
                           orphan=OrphanedCode(code=<code:1ef0d265c7e7>, …)

        (b) REFUSAL PATH   accept(auction, 'bid-t283', None, 'redirect', …)
            uninjected  -> AcceptResult(accepted=True)
            injected    -> exchange.auction.ledger.MalformedLedgerPayload escaping accept()
                           uncaught — a 500 on the served path, on precisely the input the
                           orphan machinery exists to record

    So a discount that the merchant has really issued — live, single-use, chargeable — is
    withheld from the buyer because the *record* of it would not validate. That is the exact
    inversion of the module's own rule: the audit trail decided the money path.

    **Both consumers are asserted in one node on purpose.** They are one property ("a malformed
    audit record is reported, never fatal") and one repair; splitting them would give the ticket
    two selectors and let half a fix read as done. The success path is checked first because it
    is the half that costs a buyer a real code.

    What the fix is NOT: making ``build_published_event`` stop raising. The ticket says so, and
    ``test_the_malformed_audit_record_probe_is_armed`` is the tripwire that makes it stick — it
    fails ``make verify`` if the producing-boundary check T-235 was closed with is weakened, and
    it goes on doing that after this marker is deleted. The fix is at the consumers: catch it,
    record the malformed body where an operator can find it, and hand the buyer the code that
    was minted for them.
    """
    from exchange.auction.ledger import MalformedLedgerPayload  # noqa: PLC0415
    from exchange.checkout import SimulatedRedirectProvider  # noqa: PLC0415
    from exchange.checkout.provider import OrphanedCheckoutCode  # noqa: PLC0415

    provider = SimulatedRedirectProvider()

    with _contract_publishes_one_more_key("code_created", T283_UNWRITTEN_KEY):
        try:
            result = provider.checkout(_t283_request())
        except OrphanedCheckoutCode as exc:
            orphan = getattr(exc, "orphan", None)
            pytest.fail(
                "a malformed AUDIT RECORD turned a successfully minted, live discount code "
                "into a refusal handed back to the buyer: CheckoutProvider.checkout raised "
                f"{type(exc).__name__} carrying {orphan!r}. The code exists in the merchant's "
                "system and the shopper got nothing. `_events` is inside the try at "
                "provider.py:865 whose handler says 'building the result cannot normally "
                f"fail'. Message: {exc}"
            )

        assert result.code, (
            "the checkout returned no code at all under a malformed audit record; the buyer is "
            "still worse off than the merchant, who has issued one"
        )

        try:
            accepted = _t283_accept(_t283_auction())
        except MalformedLedgerPayload as exc:
            pytest.fail(
                "a malformed audit record escaped accept() uncaught, which is a 500 on the "
                "served accept path rather than a refusal: accept() calls _refused from inside "
                "an `except` block and there is no outer try, so _orphan_record's "
                f"build_published_event takes the request down. Message: {exc}"
            )

        assert accepted is not None, "accept() returned nothing; it is supposed to return a result"


class _MovesAfterTheFirstAnswer:
    """A platform registry that answers one host, then a different one.

    Not a hostile store and not a stub for its own sake: it is the shape
    ``CheckoutProvider._mint_recording_orphans`` documents — *"a ``registered_domains`` lookup
    that answers the first call and raises on the second ... a dropped connection, a cache
    eviction"* — with the failure moved from an exception to a different ANSWER, which is what
    a registry that has been rewritten between two reads does. The port resolves the domain
    once before the mint and ``default_permalink`` resolves it again inside it, so the second
    answer is the host the permalink is built on and the first is the host it is checked
    against: an ordinary, correct ``OrphanedOffDomainCheckout`` with a live code behind it.
    """

    def __init__(self, first: str, then: str) -> None:
        self._answers = [first, then]

    def domain_for(self, store_id: str) -> str | None:
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


def _accept_through_a_post_mint_domain_refusal(registry: Any) -> Any:
    """``accept()`` on the T-283 auction with ``registry`` as the platform's lookup."""
    from exchange.accept import accept  # noqa: PLC0415 - kept out of this file's frozen import head

    return accept(
        _t283_auction(),
        "bid-t283",
        None,
        "redirect",
        registered_domains=registry,
        claims=None,
    )


def test_a_malformed_audit_record_leaves_a_refused_accept_a_refusal() -> None:
    """The ``accept()`` half of the property above, kept measurable after the checkout half.

    NOT xfail, and it is a second node rather than a second assertion for one reason: the gate
    above reaches ``_refused`` **through** the checkout defect. With ``code_created`` widened,
    ``provider.checkout`` raised ``OrphanedCheckoutCode``, ``accept()`` caught it, and
    ``_refused`` -> ``_orphan_record`` re-raised the same ``MalformedLedgerPayload`` out of an
    ``except`` block with no outer try. Repair the checkout half and that route into ``_refused``
    is gone — the checkout returns a result, ``accept()`` succeeds, and the accept half of the
    ticket stops being graded by the node that found it. Half a repair would read as done.

    This reaches ``_refused`` a way that does not depend on the audit record at all: an ordinary
    post-mint domain refusal (T-202's own shape, see :class:`_MovesAfterTheFirstAnswer`). That
    refusal is CORRECT and must stay a refusal — what must not happen is the record of the live
    code it carries turning it into an exception out of a served accept.

    Both controls the file's convention demands are here, in this node:

    * **uninjected**, the same call must refuse *and* carry the orphan's ``code_created``
      record — otherwise the probe never reaches ``_orphan_record`` and grades nothing;
    * **injected**, the refusal must survive, still name the live code on
      ``AcceptResult.orphaned_code`` (the only channel that can — the ``denial_reason`` prose
      may not spell a discount, T-215), and the audit failure must be recorded rather than
      dropped in silence.
    """
    from exchange.auction.ledger import (  # noqa: PLC0415
        MalformedLedgerPayload,
        audit_anomalies,
    )

    # CLEAN CONTROL — uninjected, this really is a post-mint refusal that files an orphan
    # record. If this stops holding, everything below is grading some other refusal.
    control = _accept_through_a_post_mint_domain_refusal(
        _MovesAfterTheFirstAnswer(T283_DOMAIN, "moved.example.com")
    )
    assert not control.accepted, (
        "the probe's registry no longer produces a refusal at all; it is supposed to move the "
        "permalink off the domain the port checked before the mint"
    )
    assert control.orphaned_code is not None, (
        f"the refusal carried no orphaned code ({control.denial_reason!r}), so it refused "
        "BEFORE anything was minted and never reaches _orphan_record"
    )
    assert "code_created" in control.kinds, (
        f"the uninjected refusal filed {control.kinds}; without the orphan's code_created "
        "record this probe is not exercising the builder T-283 is about"
    )

    seen_before = len(audit_anomalies())

    with _contract_publishes_one_more_key("code_created", T283_UNWRITTEN_KEY):
        try:
            refusal = _accept_through_a_post_mint_domain_refusal(
                _MovesAfterTheFirstAnswer(T283_DOMAIN, "moved.example.com")
            )
        except MalformedLedgerPayload as exc:
            pytest.fail(
                "a malformed audit record escaped accept() uncaught on the very path the "
                "orphan machinery exists for: a live code had already been minted, the "
                "post-mint domain check refused it, and _refused -> _orphan_record raised "
                "from inside an `except` block with no outer try — a 500 where the buyer "
                f"should have been told the offer was refused. Message: {exc}"
            )

    assert not refusal.accepted, "the refusal became an acceptance under a malformed audit record"
    assert refusal.orphaned_code is not None, (
        "the refusal lost the live discount code: `orphaned_code` is the ONLY channel that "
        "may carry it (the denial_reason prose may not, T-215), so dropping it here is the "
        "T-157/T-202 defect returning by way of the audit record"
    )
    assert refusal.orphaned_code.code, "the orphan record carries no code at all"

    recorded = audit_anomalies()[seen_before:]
    assert [anomaly.kind for anomaly in recorded] == ["code_created"], (
        "the audit failure was neither emitted nor recorded: with the code_created record "
        "refused, an anomaly naming it is the only thing standing between an operator and a "
        f"live discount nobody wrote down. Anomalies since the control: {recorded}"
    )
    assert T283_UNWRITTEN_KEY in recorded[0].problem, (
        f"the recorded anomaly does not say what was wrong with the body: {recorded[0]}"
    )


def test_the_audit_anomaly_channel_never_writes_down_a_live_code() -> None:
    """The channel T-283's repair added holds a ``code_created`` body, so it is graded as one.

    NOT xfail. The repair keeps the malformed record instead of raising it, and the body it
    keeps is the one carrying a live single-use discount — so a new place that holds that
    payload is a new place it can leak from, and this whole package is built around that not
    happening: ``OrphanedCode`` suppresses its generated ``repr``, the refusal prose is
    redacted, ``safe_token`` drops a merchant-authored fragment that spells the code (T-215).
    An anomaly is a dataclass, and a dataclass gets formatted — by a log line, a traceback, a
    REPL, this test's own failure message. It therefore keeps key NAMES and a fingerprint and
    no payload value at all.

    It is also the only node that asserts the CHECKOUT half's anomaly is recorded rather than
    silently swallowed. The ticket's node asserts the buyer keeps the code, which is the half
    that matters to the buyer; this is the half that matters to whoever has to find the
    missing record afterwards — and "dropped the event and told nobody" would satisfy that
    node exactly as well as the repair does.
    """
    from exchange.auction.ledger import audit_anomalies  # noqa: PLC0415
    from exchange.checkout import SimulatedRedirectProvider  # noqa: PLC0415

    provider = SimulatedRedirectProvider()
    seen_before = len(audit_anomalies())

    with _contract_publishes_one_more_key("code_created", T283_UNWRITTEN_KEY):
        result = provider.checkout(_t283_request())

    recorded = audit_anomalies()[seen_before:]
    assert [anomaly.kind for anomaly in recorded] == ["code_created"], (
        "the checkout dropped its code_created record without recording an anomaly for it, "
        f"which is losing the audit trail quietly rather than reporting it. Recorded: {recorded}"
    )
    anomaly = recorded[0]

    assert anomaly.where == "CheckoutProvider._events", (
        f"the anomaly is filed under {anomaly.where!r}; the call site has to be the one an "
        "operator can open, and it must be code-authored — a provider may name itself after "
        "the code it is minting"
    )
    assert T283_UNWRITTEN_KEY in anomaly.problem, (
        f"the anomaly does not name the key that was missing: {anomaly.problem!r}. That "
        "sentence is the diagnosis; without it the record says only that something was wrong"
    )
    assert "code" in anomaly.written and "checkout_token" in anomaly.written, (
        f"the anomaly does not say what the producer actually wrote ({anomaly.written}), so "
        "nobody can reconstruct the record that was refused"
    )
    # `published` is read through `published_body`, and this file's own injection docstring
    # says why it reads the UN-widened tuple here: `exchange.auction.ledger` imported
    # `LEDGER_PAYLOAD_SHAPES` by value and the injection rebinds it on `contracts.ledger`
    # only. Asserting the widened tuple would be asserting that a documented, deliberate
    # property of the harness is false — the missing key is pinned above, off `problem`,
    # which is built by the same call that raised.
    assert anomaly.published == ("code", "permalink_url", "expires_at"), (
        f"the anomaly records {anomaly.published} as the published body; an operator reading "
        "it has to be told what the shape was supposed to be"
    )
    assert anomaly.context.get("checkout_token") == result.checkout_token, (
        "the anomaly does not carry the checkout_token, so the two events that DID survive "
        "cannot be joined back to the record that did not"
    )

    rendered = f"{anomaly!r} {anomaly}"
    assert result.code and result.code not in rendered, (
        "the anomaly renders the live discount code. Every T-215 measurement in this package "
        "is about a value like this reaching something that formats it"
    )
    assert result.kinds == ["accepted", "checkout_redirect"], (
        f"the checkout filed {result.kinds}: the refused record must be ABSENT, not emitted "
        "through the unvalidated builder — a body that lies under a published kind is the "
        "one-kind-two-bodies defect T-235 closed"
    )


# =====================================================================================
# T-150 — no SERVED request feeds the trust ledger: the exchange writes into a stub
#
# READ THIS BEFORE DISPATCHING A LANE AGAINST THIS GATE. ``tickets.json`` carries
# ``"status": "closed"`` on T-150 with ``status_evidence``: *"REFUTED by the cycle-10 triage:
# 'apps/exchange/src/auction/ledger.py:54 — the ledger writer having zero producers' is not a
# defect. Same class ratified by T-176."* ``.swarm-loop/backlog.md`` still lists it as an open
# NO-GATE item, so the graph disagrees with itself. The gate below is written because the
# MEASUREMENT holds — a served auction reaches no ledger — and because a refuted ticket that
# nobody can re-measure is how a ruling outlives the reason for it. It is xfail, so it costs
# ``make verify`` nothing while the ruling stands. If the ruling is upheld, delete this whole
# section rather than weakening it; a gate whose ticket no lane may act on is one that rots.
# The other two gates in this file are independent of it.
# =====================================================================================


def _is_ledger_post(request: Any) -> bool:
    """Does this outbound request look like an append to the trust ledger?

    ``POST /events`` is what ``packages/contracts/openapi/trust.openapi.json`` publishes and the
    only operation ``trust.main`` and its contract agree on, so it is the wire shape a producer
    would have to use. The suffix match keeps a deployment that mounts the service under a
    prefix (``/trust/events``) from reading as "no producer".
    """
    path = str(request.url.path).rstrip("/")
    return str(request.method).upper() == "POST" and path.endswith("/events")


@contextmanager
def _trust_ledger_writes_observed() -> Iterator[list[str]]:
    """Watch BOTH channels by which a ledger event could leave this process for trust.

    T-150's claim is "not by import, not over HTTP", so both are watched:

    * **by import** — every trust event store's ``append``, patched on the CLASS. That is the
      funnel: ``trust.events.append(store, event)`` is documented as "the one entry point,
      whatever the store is" and it does nothing but delegate to ``store.append(event)``, so a
      producer is caught however it reached the function. Patching the module attribute instead
      would be defeated by an ordinary ``from trust.events import append`` executed before this
      test ran, which is exactly what a fix inside ``ledger.py`` would write; method lookup on a
      class is late-bound and has no such hole.
    * **over HTTP** — ``httpx.Client.send`` / ``httpx.AsyncClient.send``, also late-bound.
      ``httpx`` is the repo's only HTTP client.

    ``TestClient`` is an ``httpx.Client`` subclass, so the probe's OWN request to the exchange
    would otherwise be recorded as an outbound call and, worse, be short-circuited before it
    reached the app. It is delegated to the original ``send`` by identity check. Everything else
    is answered with a synthetic 202 and never touches a socket: a fix pointed at a hostname
    that does not exist must be OBSERVED, not turned into a DNS lookup from the test suite.

    ``PostgresEventStore`` is patched alongside the in-memory one and importing it costs only
    ``psycopg``'s import, no connection. Leaving it out would mean a producer wired to the real
    D16 store — the one a deployment uses — read as no producer at all.
    """
    import httpx  # noqa: PLC0415 - kept out of this file's frozen import head
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from trust.events.pg import PostgresEventStore  # noqa: PLC0415
    from trust.events.store import InMemoryEventStore  # noqa: PLC0415

    observed: list[str] = []

    original_send = httpx.Client.send
    original_asend = httpx.AsyncClient.send
    original_memory_append = InMemoryEventStore.append
    original_pg_append = PostgresEventStore.append

    def _record_request(request: Any) -> Any:
        label = "ledger-post" if _is_ledger_post(request) else "other-http"
        observed.append(f"{label} {request.method} {request.url}")
        return httpx.Response(202, request=request, json={})

    def _send(self: Any, request: Any, **kwargs: Any) -> Any:
        if isinstance(self, TestClient):
            return original_send(self, request, **kwargs)
        return _record_request(request)

    async def _asend(self: Any, request: Any, **kwargs: Any) -> Any:
        if isinstance(self, TestClient):
            return await original_asend(self, request, **kwargs)
        return _record_request(request)

    def _memory_append(self: Any, event: Any) -> Any:
        observed.append(f"append InMemoryEventStore kind={_kind_of(event)!r}")
        return original_memory_append(self, event)

    def _pg_append(self: Any, event: Any) -> Any:
        observed.append(f"append PostgresEventStore kind={_kind_of(event)!r}")
        return original_pg_append(self, event)

    httpx.Client.send = _send
    httpx.AsyncClient.send = _asend
    InMemoryEventStore.append = _memory_append
    PostgresEventStore.append = _pg_append
    try:
        yield observed
    finally:
        httpx.Client.send = original_send
        httpx.AsyncClient.send = original_asend
        InMemoryEventStore.append = original_memory_append
        PostgresEventStore.append = original_pg_append


def _kind_of(event: Any) -> str:
    """``event['kind']`` for anything mapping-shaped, and never an exception in an observer."""
    try:
        return str(event["kind"])
    except Exception:  # pragma: no cover - a producer may hand the store anything at all
        return "<unreadable>"


def _ledger_writes(observed: list[str]) -> list[str]:
    """The entries in ``observed`` that are a ledger event LEAVING for trust."""
    return [entry for entry in observed if not entry.startswith("other-http ")]


def _serve_one_auction() -> dict[str, Any]:
    """Run one real auction through the app ``exchange.main`` builds, and report what it did.

    The wiring is the MINIMUM a deployment must supply and nothing more: an eligibility source
    and a solicitor, without which every store is denied ``unavailable`` and the auction
    collects no bid at all. The ledger is deliberately **not** wired — ``configure_auctions``
    takes a whole ``machine`` and a deployment could hand one over with a real sink on it, which
    is one shape a fix might take, and a probe that passed its own sink in would be answering
    the question it is asking.
    """
    from exchange.auction.routes import configure_auctions  # noqa: PLC0415
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility  # noqa: PLC0415
    from exchange.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    roster = [{"store_id": f"store-{index}", "list_price": 100.0} for index in range(1, 5)]

    class _Solicitor:
        """A store agent that answers every ask with an honest under-list bid."""

        def solicit(self, store: Any) -> Any:
            store_id = str(store["store_id"])
            return {
                "store_id": store_id,
                "bid": {
                    "bid_id": f"bid-{store_id}",
                    "bid_ref": f"bid-{store_id}",
                    "store_id": store_id,
                    "store_domain": f"{store_id}.example.com",
                    "offer": {
                        "product_ref": "prod-1",
                        "unit_price": 88.0,
                        "total_price": 88.0,
                        "discount": 0.12,
                        "quantity": 1,
                    },
                },
            }

    app = create_app()
    configure_auctions(
        app,
        solicitor=_Solicitor(),
        eligibility=StaticSellerEligibility({str(entry["store_id"]): ELIGIBLE for entry in roster}),
    )
    client = TestClient(app)
    response = client.post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-t150", "query": "merino socks", "cluster_id": "c-1"},
            "roster": roster,
        },
    )
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    machine = getattr(app.state, "auction_machine", None)
    recorder = getattr(machine, "ledger", None)
    sink = getattr(recorder, "sink", None)
    return {
        "status": response.status_code,
        "state": str(body.get("state") or ""),
        "auction_id": str(body.get("auction_id") or ""),
        "sink": type(sink).__name__ if sink is not None else "<none>",
        "sink_kinds": list(getattr(sink, "kinds", []) or []),
    }


def test_the_trust_ledger_producer_probe_is_armed() -> None:
    """NOT xfail. Five ways the T-150 gate could report "no producer" while measuring nothing.

    * **The auction never runs.** ``POST /auctions`` answering 422 because the request schema
      moved, or 500 because the app would not build, produces no ledger event at all — and a
      gate whose subject never happened reads exactly like a gate whose subject was not
      recorded. The probe's own response and the auction's final state are asserted.
    * **The state machine emits nothing to begin with.** Asserted on a machine this test owns
      and drives by hand, so the number can never be zero for a reason the gate would misread —
      and asserted WITHOUT touching the served app's wiring, which is the thing under test.
    * **The observer sees nothing because it is broken.** Two planted positives, one per
      channel: a real ``trust.events.append`` into a real hash-chained store, and a real
      ``httpx`` POST to a ``/events`` path. Both must be recorded.
    * **The observer sees everything.** A clean control emits an event into the exchange's own
      ``InMemoryLedgerSink`` — precisely what the code does today — and the observer must record
      nothing for it. Without this the gate would go green the moment anything at all happened.
    * **The observer outlives the test.** ``httpx.Client.send`` and both store ``append``
      methods are patched process-wide; a leak would silently rewrite every later HTTP test in
      the session. Restoration is asserted by identity.
    """
    import exchange.main as main_module  # noqa: PLC0415 - kept out of this file's frozen import head
    import httpx  # noqa: PLC0415
    import trust.events.store as trust_store_module  # noqa: PLC0415
    from exchange.auction import (  # noqa: PLC0415
        AuctionStateMachine,
        InMemoryAuctionStore,
        InMemoryLedgerSink,
    )
    from exchange.auction.ledger import build_event  # noqa: PLC0415
    from trust.events import InMemoryEventStore, append  # noqa: PLC0415

    # `trust.events.InMemoryEventStore is trust.events.store.InMemoryEventStore` -> True
    # (measured). The two spellings of this package are one module by T-119/T-126's elected
    # primary, so patching the class through either name is the same patch — which is what
    # makes the restoration check below meaningful rather than a check on an alias.
    stray = _resolves_inside_the_tree(main_module, trust_store_module)
    assert not stray, (
        f"these modules resolved outside the tree under test ({REPO_ROOT}): {stray} — that is "
        "the _proxyshop.pth leak, not a measurement of this branch"
    )

    # An auction is arithmetically incapable of emitting zero ledger events. Measured on a
    # machine this test owns, so the served app's wiring — the subject of the gate — is not
    # consulted to arm the gate.
    owned_sink = InMemoryLedgerSink()
    machine = AuctionStateMachine(InMemoryAuctionStore(), owned_sink)
    machine.create("auction-armed", intent_id="intent-armed", cluster_id="c-1", roster=[])
    machine.open("auction-armed", now=T283_NOW)
    machine.close("auction-armed", now=T283_NOW + 1.0)
    assert len(owned_sink.events) >= 2, (
        f"driving an auction through the state machine emitted {owned_sink.kinds}; with fewer "
        "than two transitions recorded there is nothing for a producer to forward and the gate "
        "below would be asserting about an empty stream"
    )

    served = _serve_one_auction()
    assert served["status"] == 201, (
        f"POST /auctions answered {served['status']}, so the gate below never ran an auction at "
        f"all: {served}"
    )
    assert served["state"] == "closed", (
        f"the served auction ended in state {served['state']!r} rather than 'closed'; it did "
        f"not transition, so it produced no ledger events to forward: {served}"
    )

    before_send = httpx.Client.send
    before_append = InMemoryEventStore.append

    event = build_event(
        "auction_opened",
        auction_id="auction-armed",
        payload={"intent_id": "intent-armed", "cluster_id": "c-1", "roster_size": 0},
    )

    with _trust_ledger_writes_observed() as observed:
        # CLEAN CONTROL — what the exchange does today. The observer must record NOTHING for it,
        # or "no producer" would be indistinguishable from "the observer matches everything".
        InMemoryLedgerSink().emit(event)
        assert observed == [], (
            f"emitting into the exchange's own in-memory stub was recorded as a trust-ledger "
            f"write: {observed}. The observer matches everything and proves nothing"
        )

        # PLANTED POSITIVE 1 — the import channel, through the documented entry point and into a
        # real hash-chained store.
        outcome = append(InMemoryEventStore(), event)
        assert outcome.inserted and outcome.head_hash, (
            "trust.events.append did not chain the exchange's own build_event output; the "
            "planted positive is not a real ledger write"
        )
        assert len(_ledger_writes(observed)) == 1, (
            f"a real trust.events.append was not observed: {observed}. The gate below could not "
            "tell an in-process producer from no producer at all"
        )

        # PLANTED POSITIVE 2 — the HTTP channel. Answered synthetically; no socket is touched.
        response = httpx.Client().post("http://trust.invalid/events", json=event)
        assert response.status_code == 202, (
            f"the observer did not answer the planted outbound POST (got {response.status_code})"
        )
        assert len(_ledger_writes(observed)) == 2, (
            f"a real outbound POST to a /events path was not observed as a ledger write: {observed}"
        )

        # ...and the observer still discriminates: a POST that is not an append is not one.
        httpx.Client().post("http://stores.invalid/bids", json={})
        assert len(_ledger_writes(observed)) == 2, (
            f"an unrelated outbound POST was counted as a trust-ledger write: {observed}"
        )

    assert httpx.Client.send is before_send, "the httpx observer outlived its context manager"
    assert InMemoryEventStore.append is before_append, (
        "the trust event-store observer outlived its context manager; every later test in this "
        "session is now recording into a list this file owns"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-150 (NARROWED — the ticket's absolute claim is false at HEAD, see the docstring): "
        "the SERVED exchange has no producer into the trust ledger. MEASURED — POST /auctions "
        "on the app exchange.main.create_app() builds answers 201 and drives the auction to "
        "'closed', emitting auction_opened and auction_closed into the InMemoryLedgerSink that "
        "auction/routes.py:382's bare AuctionStateMachine() defaults to (ledger.py:172); with "
        "every trust event store's append and every outbound httpx request watched, nothing "
        "left the process by either channel. The once-only landing, hash chaining and replay "
        "in apps/trust/src/events grade a stream no SERVED request produces; remove this "
        "marker with the fix"
    ),
)
def test_t150_a_served_auction_puts_its_transitions_in_the_trust_ledger() -> None:
    """An auction the exchange serves must leave a record in the ledger that grades it.

    Measured at HEAD by building the app and posting one auction at it::

        POST /auctions                       -> 201, state 'closed'
        app.state.auction_machine.ledger     -> LedgerRecorder
        …ledger.sink                         -> InMemoryLedgerSink
        …sink.kinds                          -> ['auction_opened', 'auction_closed']
        trust event-store appends observed   -> 0
        outbound POSTs to a /events path     -> 0

    ``InMemoryLedgerSink``'s own docstring calls it "the trust API stub … the tests and the
    default service wiring use", and ``auction/routes.py:382`` builds a bare
    ``AuctionStateMachine()`` per app, whose ``LedgerRecorder`` (``ledger.py:172``) constructs
    one of those stubs when no sink is handed over. Nothing hands one over: the only
    constructions of the machine with a sink in the tree are ``services/sim/src/runner.py:560``
    and ``e2e/support/s1/flow.py:359``, and both pass another ``InMemoryLedgerSink``. So the
    events are appended to a list that is unreachable from outside the process and discarded
    with the app.

    That is not a gap in ``apps/trust``: its writer is built, and the exchange's own
    ``build_event`` output lands in it unmodified — measured, ``append(InMemoryEventStore(),
    build_event('auction_opened', …))`` returned ``inserted=True, seq=1`` with a chain head. The
    writer is simply unfed by anything a request reaches.

    **The ticket's claim as written is FALSE at HEAD, and this gate is deliberately narrower
    than it.** T-150 says "Nothing in the repository writes an event into it — not by import,
    not over HTTP", and gives a grep as its reproduction. Run at HEAD that grep does NOT come
    back empty::

        $ grep -rn --include='*.py' -e 'trust\\.events' -e 'apps\\.trust\\.src\\.events' . \\
            | grep -v /\\.venv/ | grep -v '^\\./apps/trust/' | grep -v '^\\./\\.swarm-loop/'
        e2e/support/s1/flow.py:920:      from trust.events import InMemoryEventStore, append
        services/sim/src/runner.py:523:  from trust.events import InMemoryEventStore, append
        …

    and both are real producers, not mentions: ``services/sim/src/runner.py:660`` runs
    ``append(event_store, event)`` over "every event the exchange emitted" for every simulation
    episode, and ``e2e/support/s1/flow.py:924`` chains the whole S1 run through the same seam
    and verifies it. So the writer HAS been fed, by an offline simulation harness and by the
    e2e support module. What no part of the tree does — and what this gate asserts — is feed it
    from a request the exchange actually serves: the sim builds its own state machine and its
    own store side by side and bridges them itself, which is precisely why nothing about
    ``exchange.main``'s wiring had to change for it to work. Stating the property as "a served
    auction reaches the ledger" is what keeps this gate red for the real defect while being
    honest about what the ticket over-claimed.

    Corrected line numbers, since the ticket's are stale: the defect it points at,
    ``ledger.py:54``, is inside :class:`MalformedLedgerPayload`'s docstring today;
    ``InMemoryLedgerSink`` is at ``ledger.py:74`` and the default that installs it is at ``:172``.

    **This gate observes an EVENT ARRIVING, never source text.** A "producer exists" check that
    grepped for ``trust.events`` would be satisfied by writing the string in a comment. What is
    watched instead is the two ways an event can actually leave: every trust event store's
    ``append``, patched on the class so the funnel ``trust.events.append`` delegates through is
    caught however a fix imported it, and every outbound ``httpx`` request. It is deliberately
    agnostic about which channel a repair uses and about how the sink is wired — a real
    ``LedgerSink`` implementation handed to ``AuctionStateMachine``, an HTTP client posting to
    the published ``POST /events``, or a deployment passing its own machine through
    ``configure_auctions`` all satisfy it. What it cannot be satisfied by is a comment, a
    docstring, an ``__all__`` entry, or a producer that exists and is not reached by the served
    path.

    Fakes it IS resistant to: source-text mentions of ``trust.events`` anywhere; a producer
    module that exists but nothing imports; a producer reached only from a test or from the sim
    harness; renaming ``InMemoryLedgerSink``; wiring a sink that stores events elsewhere in
    this process.
    Fakes it is NOT resistant to: a sink that makes one HTTP POST to a ``/events`` path and
    throws the response away, or one that appends a fabricated event to a store nobody reads.
    Both are real writes into the trust ledger's own writer, which is the property the ticket
    names; grading what happens to the event afterwards is T-061's reconcile ticket, not this
    one.
    """
    with _trust_ledger_writes_observed() as observed:
        served = _serve_one_auction()

    writes = _ledger_writes(observed)
    assert writes, (
        "an auction served end to end by exchange.main.create_app() put no event into the trust "
        f"ledger by any channel — {served['sink_kinds']} were recorded into "
        f"{served['sink']}, an in-process list that is discarded with the app, and nothing was "
        f"appended to a trust event store or POSTed to a /events path. Observed traffic: "
        f"{observed or 'none at all'}. The hash chaining, once-only landing and replay in "
        "apps/trust/src/events are grading a stream no served request produces — the sim "
        "harness bridges its own events across by hand, and the deployed service does not."
    )
