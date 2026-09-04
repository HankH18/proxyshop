"""Reproductions for the cycle-16 rung-2 verifier's trust findings — T-275.

The gate here arrived ``xfail(strict=True)`` and asserts the behaviour that SHOULD hold, so a
normal run reports ``xfailed`` and the repo-wide build gate stays green, while the ticket's own
gate runs the node under ``--runxfail`` and gets a real failure.

Its subject is another gate rather than product code, and that is the finding: a strict gate whose
detector cannot see a correct fix is a ticket that can never close, and one that can be satisfied
by a comment is a ticket that can close without one.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-275: T-237's _KIND_EMISSION matches only QUOTED STRING literals, so a blacklisting "
        "written in this repo's own ledger-writer house style — kind=LedgerEventKind.blacklisted, "
        "as apps/buyer/svc/src/feedback/submission.py:442 writes kind=LedgerEventKind.feedback — "
        "does not match, and a correct fix leaves that strict=True gate red forever; the same "
        "pattern also matches a COMMENT or DOCSTRING carrying the literal, so the gate can go "
        "green with no code change at all; remove this marker with the fix"
    ),
)
def test_t275_the_t237_emission_detector_reads_code_and_not_spellings() -> None:
    """T-237's gate has to recognise a fix, and has to refuse a fake one.

    ``_KIND_EMISSION`` is the whole of what
    ``test_a_sub_threshold_trust_score_produces_a_blacklisted_ledger_event`` means by "a product
    module emits a blacklisted ledger event": it is searched against the raw text of every product
    ``.py`` file, and the gate passes when any file matches. Both of its failure directions are
    live at once.

    **It cannot see a real fix.** All three alternatives require the kind to be a quoted string —
    ``"kind": "blacklisted"``, ``kind="blacklisted"``, ``_KIND = "blacklisted"``. The repo's
    ledger writers do not spell it that way. ``apps/buyer/svc/src/feedback/submission.py:442``
    builds its event with ``kind=LedgerEventKind.feedback``, the typed member off the frozen
    ``LedgerEventKind`` enum, which is the house style precisely because it is the spelling a
    typo cannot survive. A blacklisting emitted the same way matches nothing, so the fix lands,
    the defect closes, and the strict gate stays red — and a strict gate that stays red after its
    own fix is a ticket that cannot be closed by anyone.

    **It can be satisfied without a fix.** The search runs over ``path.read_text()``, so a
    ``#`` comment or a docstring containing the literal matches exactly as well as an emission
    does. Someone writing *about* blacklisting — this test file's own subject matter, for
    instance — greens a gate that asserts nothing happened.

    A detector that answers both of these correctly is asking about the code, not about how the
    code is spelled. This test is the assertion of that, in the smallest form that distinguishes
    the two: the enum spelling must match, and prose must not.
    """
    from apps.trust.tests import test_repro_open_tickets as gate  # noqa: PLC0415

    detector = getattr(gate, "_KIND_EMISSION", None)
    assert detector is not None, (
        "T-237's gate no longer exposes an emission detector under a name this test can find. "
        "It needs one: the ticket's whole assertion is 'some product module emits a blacklisted "
        "ledger event', and that question has to be answerable by something other than a regex "
        "over quoted strings."
    )

    enum_emission = "    event = LedgerEvent(ts=ts, kind=LedgerEventKind.blacklisted, store_id=s)\n"
    assert detector.search(enum_emission), (
        "the T-237 gate cannot see a blacklisting emitted in this repo's own ledger-writer house "
        f"style: {enum_emission!r} matches nothing, so a correct fix written the way "
        "apps/buyer/svc/src/feedback/submission.py writes its own kinds leaves a strict=True "
        "gate red forever and T-237 can never be closed."
    )

    prose_only = (
        '# Nothing here emits {"kind": "blacklisted"} yet — see T-237 for the missing seam.\n'
        '"""A docstring that merely mentions kind="blacklist_expired" is not an emission."""\n'
    )
    assert not detector.search(prose_only), (
        "the T-237 gate is satisfied by a COMMENT and a DOCSTRING that merely name the frozen "
        "kinds, so it can be turned green without any product code emitting anything: "
        f"{prose_only!r}"
    )
