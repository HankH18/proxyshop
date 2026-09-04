"""One module object per file, across both dotted spellings of this tree (T-073).

``apps/buyer/svc/src`` is importable as ``buyer_svc.<mod>`` (through the tracked
``.pkgroot/buyer_svc`` symlink) and as ``apps.buyer.svc.src.<mod>`` (the repo-root path the frozen
acceptance suite uses). Python will happily execute every file TWICE, once per spelling, and the
duplication is invisible until something in the package holds identity or state — which this one
does, in both forms:

* the submission ledger exists twice, so "one routed order, one feedback event" holds only per
  spelling — and a pytest session running the frozen acceptance suite beside the FastAPI app is
  exactly a process that reaches ``submit_feedback`` both ways. Two feedback events for one
  purchase is a doubled observation on the store's ``feedback_match`` dimension, which is the
  dimension R14's routing gate exists to protect;
* ``FeedbackAlreadySubmitted`` raised through one spelling is not caught by an ``except`` written
  against the other, so the route module's 409 branch would miss the refusal and answer 500.

Measured on this worktree before ``_spellings.bind_package`` was added to this package:
``apps.buyer.svc.src.feedback is buyer_svc.feedback`` was ``False``.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import buyer_svc.feedback as pkgroot_spelling
import pytest

import apps.buyer.svc.src.feedback as repo_root_spelling
from apps.buyer.svc.src.feedback._spellings import SPELLINGS, bind_spellings

SUBMODULES = ("_reading", "errors", "prompt", "routing", "submission")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: Every order in which the two spellings can arrive. Each runs in a FRESH interpreter, because
#: import order is process state and an in-process test can only ever observe the one order this
#: session happened to take.
IMPORT_ORDERINGS = (
    "import buyer_svc.feedback; import apps.buyer.svc.src.feedback as p",
    "import apps.buyer.svc.src.feedback; import buyer_svc.feedback as p",
    "import buyer_svc.feedback.routes; import apps.buyer.svc.src.feedback.routes as p",
    "import apps.buyer.svc.src.feedback.routes; import buyer_svc.feedback.routes as p",
    "import buyer_svc.main; import apps.buyer.svc.src.feedback as p",
    "from apps.buyer.svc.src.feedback import submit_feedback; import buyer_svc.feedback as p",
)

ORDER = {
    "order_ref": "ord-spell-1",
    "store_id": "st-1",
    "auction_id": "auc-spell-1",
    "routed": True,
}
ANSWER = {"question_id": "matched_pitch", "choice": "yes_as_described"}


def test_both_spellings_are_the_same_package_object() -> None:
    assert repo_root_spelling is pkgroot_spelling


@pytest.mark.parametrize("name", SUBMODULES)
def test_both_spellings_are_the_same_submodule_object(name) -> None:
    assert getattr(repo_root_spelling, name) is getattr(pkgroot_spelling, name)


def test_a_refusal_raised_through_one_spelling_is_caught_through_the_other() -> None:
    """The failure this file exists for: two classes, same line, neither catching the other."""
    with pytest.raises(pkgroot_spelling.UnusableOrder):
        repo_root_spelling.feedback_prompt("not-an-order")
    with pytest.raises(repo_root_spelling.FeedbackError):
        pkgroot_spelling.submit_feedback(ORDER, {"choice": "nonsense"}, [].append)


def test_there_is_exactly_one_submission_ledger() -> None:
    """Two ledgers would make "one routed order, one feedback event" true only per spelling."""
    assert repo_root_spelling.submitted() is pkgroot_spelling.submitted()

    seen: list[object] = []
    repo_root_spelling.reset_submitted()
    try:
        pkgroot_spelling.submit_feedback(ORDER, ANSWER, seen.append)
        with pytest.raises(repo_root_spelling.FeedbackAlreadySubmitted):
            repo_root_spelling.submit_feedback(ORDER, ANSWER, seen.append)
        assert len(seen) == 1
    finally:
        repo_root_spelling.reset_submitted()


@pytest.mark.parametrize("program", IMPORT_ORDERINGS)
def test_identity_holds_whichever_spelling_is_imported_first(program) -> None:
    """In-process this session took ONE order; these take the other five, freshly."""
    script = (
        f"{program}\n"
        "import apps.buyer.svc.src.feedback as a, buyer_svc.feedback as b\n"
        "assert a is b, 'the two spellings are different module objects'\n"
        "assert a.FeedbackAlreadySubmitted is b.FeedbackAlreadySubmitted\n"
        "assert a.submitted() is b.submitted()\n"
        "assert a.CHOICES_BY_ID is b.CHOICES_BY_ID\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("ok")


def test_binding_touches_only_this_trees_two_spellings() -> None:
    """`bind_spellings` is not a general aliasing facility, and this is what says so."""
    import json as unrelated

    assert bind_spellings(unrelated) == ()
    assert SPELLINGS == ("buyer_svc", "apps.buyer.svc.src")
