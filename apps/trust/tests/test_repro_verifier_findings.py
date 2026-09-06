"""Reproductions for the cycle-16 rung-2 verifier's trust findings — T-275.

Its subject is another gate rather than product code, and that is the finding: a strict gate whose
detector cannot see a correct fix is a ticket that can never close, and one that can be satisfied
by a comment is a ticket that can close without one.

The node below arrived ``xfail(strict=True)``. **That marker is gone, and deliberately so.** Two
things were wrong with it at once:

* the node called ``_KIND_EMISSION.search(...)`` as if the detector were a compiled regex. On this
  tree it is a plain ``def _KIND_EMISSION(source: str) -> bool``, so the call raised
  ``AttributeError`` — and under ``xfail(strict=True)`` a RAISING test is still reported ``xfailed``,
  never ``XPASS``. The node therefore looked like a healthy "defect still open" marker while
  grading nothing, and T-275 could never be observed as closed no matter what anyone fixed; and
* T-275 is in fact FIXED on this tree. ``_KIND_EMISSION`` is now an AST reader delegating to
  ``_t302_kinds_named_in``, and its own ticket gate
  (``pytest apps/trust/tests/test_repro_open_tickets.py --runxfail -k t275``) passes. With the call
  corrected the node passes, which under ``strict=True`` is an ``XPASS`` **failure**.

So this is now an ordinary passing gate that goes red if the detector regresses to a text regex.
"""

from __future__ import annotations

import ast


def _detects(detector: object, text: str) -> bool:
    """Ask the detector about ``text`` WITHOUT pinning how it is implemented.

    T-275's remedy was "read emissions from the AST", whose natural shape is a function, not a
    compiled pattern. Hard-coding either shape is what broke this node before: ``.search`` against
    a function raises, and a raising test under ``xfail(strict=True)`` reports ``xfailed`` rather
    than failing, so the dead gate was invisible. Accept both shapes and grade the BEHAVIOUR — a
    regression to the old regex is then reported by the assertions below, on their own terms.
    """
    search = getattr(detector, "search", None)
    if search is not None:
        return bool(search(text))
    assert callable(detector), (
        f"the T-237 emission detector is neither a pattern nor callable: {detector!r}"
    )
    return bool(detector(text))


def test_t275_the_t237_emission_detector_reads_code_and_not_spellings() -> None:
    """T-237's gate has to recognise a fix, and has to refuse a fake one.

    ``_KIND_EMISSION`` is the whole of what
    ``test_a_sub_threshold_trust_score_produces_a_blacklisted_ledger_event`` means by "a product
    module emits a blacklisted ledger event": it is asked about the source of every product ``.py``
    file, and the gate passes when any file matches. Both failure directions were live at once when
    it was a whole-file regex over three quoted spellings.

    **It could not see a real fix.** All three alternatives required the kind to be a quoted string
    — ``"kind": "blacklisted"``, ``kind="blacklisted"``, ``_KIND = "blacklisted"``. The repo's
    ledger writers do not spell it that way. ``apps/buyer/svc/src/feedback/submission.py`` builds
    its event with ``kind=LedgerEventKind.feedback``, the typed member off the frozen
    ``LedgerEventKind`` enum, which is the house style precisely because it is the spelling a typo
    cannot survive. A blacklisting emitted the same way matched nothing, so the fix would land, the
    defect would close, and the strict gate would stay red — and a strict gate that stays red after
    its own fix is a ticket nobody can close.

    **It could be satisfied without a fix.** The search ran over ``path.read_text()``, so a ``#``
    comment or a docstring containing the literal matched exactly as well as an emission did.
    Someone writing *about* blacklisting — this file's own subject matter, for instance — greened a
    gate that asserts nothing happened.

    A detector that answers both correctly is asking about the code, not about how the code is
    spelled. This is the assertion of that, in the smallest form that distinguishes the two: the
    enum spelling must match, and prose must not.
    """
    from apps.trust.tests import test_repro_open_tickets as gate  # noqa: PLC0415

    detector = getattr(gate, "_KIND_EMISSION", None)
    assert detector is not None, (
        "T-237's gate no longer exposes an emission detector under a name this test can find. "
        "It needs one: the ticket's whole assertion is 'some product module emits a blacklisted "
        "ledger event', and that question has to be answerable by something other than a regex "
        "over quoted strings."
    )

    # Both samples are whole MODULES, because that is the only thing the detector is ever handed:
    # its one production call site passes `path.read_text()`. An AST reader returns "nothing named"
    # for a source it cannot parse, so a sample that is a bare indented FRAGMENT is rejected for a
    # SyntaxError rather than on its merits — which grades nothing and reads exactly like a real
    # miss. The control below pins that, so neither half of this gate can ever go quiet that way.
    enum_emission = (
        "def _delist(store_id: str, ts: str) -> None:\n"
        "    event = LedgerEvent(ts=ts, kind=LedgerEventKind.blacklisted, store_id=store_id)\n"
    )
    prose_only = (
        '# Nothing here emits {"kind": "blacklisted"} yet — see T-237 for the missing seam.\n'
        '"""A docstring that merely mentions kind="blacklist_expired" is not an emission."""\n'
    )
    for label, sample in (("enum_emission", enum_emission), ("prose_only", prose_only)):
        try:
            ast.parse(sample)
        except SyntaxError as exc:  # pragma: no cover - the control, not the measurement
            raise AssertionError(
                f"the {label} sample is not parseable Python ({type(exc).__name__}: {exc.msg}), so "
                f"an AST-reading detector reports 'no kinds named' for a reason that has nothing to "
                f"do with what this gate measures. Samples must be whole modules, the way the "
                f"detector's real caller reads whole files."
            ) from exc

    assert _detects(detector, enum_emission), (
        "the T-237 gate cannot see a blacklisting emitted in this repo's own ledger-writer house "
        f"style: {enum_emission!r} matches nothing, so a correct fix written the way "
        "apps/buyer/svc/src/feedback/submission.py writes its own kinds leaves a strict=True "
        "gate red forever and T-237 can never be closed."
    )

    assert not _detects(detector, prose_only), (
        "the T-237 gate is satisfied by a COMMENT and a DOCSTRING that merely name the frozen "
        "kinds, so it can be turned green without any product code emitting anything: "
        f"{prose_only!r}"
    )
