"""One module object per file, across both dotted spellings of this tree (T-071).

`apps/buyer/svc/src` is importable as `buyer_svc.<mod>` (through the tracked
`.pkgroot/buyer_svc` symlink) and as `apps.buyer.svc.src.<mod>` (the repo-root path the
frozen acceptance suite uses). Python will happily execute every file TWICE, once per
spelling, and the duplication is invisible until something in the package holds identity
or state — which this one does, in both forms:

* `ConfirmationWithheld` raised through one spelling is not caught by an `except` written
  against the other;
* the confirmation ledger exists twice, so "one confirmation opens one auction" holds only
  per spelling, and a process that reaches `confirm` both ways — a pytest session running
  the frozen acceptance suite beside the FastAPI app is exactly that — opens two auctions
  for one intent with every assertion still green.

`test_confirming_through_the_other_spelling_is_still_refused` is the one that fails
without `_spellings.bind_package`; everything else here explains why.
"""

from __future__ import annotations

import importlib
import pathlib
import subprocess
import sys
import types

import buyer_svc.intent as pkgroot_spelling
import pytest

import apps.buyer.svc.src.intent as repo_root_spelling
from apps.buyer.svc.src.intent._spellings import SPELLINGS, bind_package, bind_spellings

SUBMODULES = ("clarifier", "confirmation", "errors", "extraction", "models", "routes")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: Every order in which the two spellings can arrive. Each runs in a FRESH interpreter,
#: because import order is process state and an in-process test can only ever observe the
#: one order this session happened to take.
IMPORT_ORDERINGS = (
    "import buyer_svc.intent; import apps.buyer.svc.src.intent as p",
    "import apps.buyer.svc.src.intent; import buyer_svc.intent as p",
    "import buyer_svc.intent.routes; import apps.buyer.svc.src.intent.routes as p",
    "import apps.buyer.svc.src.intent.routes; import buyer_svc.intent.routes as p",
    "import buyer_svc.main; import apps.buyer.svc.src.intent as p",
    "from apps.buyer.svc.src.intent import clarify; import buyer_svc.intent as p",
)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def create_auction(self, payload):
        self.calls.append(payload)
        return {"auction_id": "auc-spelling-1"}


def test_both_spellings_are_the_same_package_object() -> None:
    assert repo_root_spelling is pkgroot_spelling


@pytest.mark.parametrize("name", SUBMODULES)
def test_both_spellings_are_the_same_submodule_object(name: str) -> None:
    left = importlib.import_module(f"apps.buyer.svc.src.intent.{name}")
    right = importlib.import_module(f"buyer_svc.intent.{name}")
    assert left is right, f"{name} was executed twice, once per spelling"


def test_the_attribute_half_of_the_binding_is_there_too() -> None:
    """A ``sys.modules`` entry alone does not make ``pkg.sub`` work; the attribute does."""
    for name in SUBMODULES:
        assert getattr(repo_root_spelling, name, None) is not None, name
        assert getattr(pkgroot_spelling, name, None) is not None, name


def test_there_is_exactly_one_of_every_exception_class() -> None:
    """``except X`` written against one spelling must catch the other spelling's raise."""
    for name in (
        "ConfirmationWithheld",
        "IntentAlreadyConfirmed",
        "UnstructuredIntent",
        "AuctionClientUnusable",
        "EmptyDialogue",
        "IntentError",
    ):
        assert getattr(repo_root_spelling, name) is getattr(pkgroot_spelling, name), name


def test_there_is_exactly_one_confirmation_ledger() -> None:
    assert repo_root_spelling.confirmations() is pkgroot_spelling.confirmations()


def test_confirming_through_the_other_spelling_is_still_refused() -> None:
    """The behavioural consequence, and the reason `_spellings.py` exists.

    With two module objects this passed the first confirm, passed the second, and opened
    TWO auctions for one buyer's one need - every assertion in every other test still
    green, because no other test crosses the spelling boundary.
    """
    repo_root_spelling.reset_confirmations()
    try:
        intent = repo_root_spelling.clarify(["I want a light roast under $20"]).intent
        first, second = Recorder(), Recorder()

        repo_root_spelling.confirm(intent, first, confirmed=True)
        with pytest.raises(pkgroot_spelling.IntentAlreadyConfirmed):
            pkgroot_spelling.confirm(intent, second, confirmed=True)

        assert len(first.calls) == 1
        assert second.calls == [], "the other spelling opened a second auction for one intent"
    finally:
        repo_root_spelling.reset_confirmations()


def test_a_refusal_raised_by_one_spelling_is_caught_by_the_other() -> None:
    with pytest.raises(repo_root_spelling.ConfirmationWithheld):
        pkgroot_spelling.confirm({}, Recorder(), confirmed=False)


def test_the_binder_leaves_unrelated_modules_alone() -> None:
    stranger = types.ModuleType("definitely_not_buyer_svc.thing")
    assert bind_spellings(stranger) == ()
    assert "definitely_not_buyer_svc.thing" not in sys.modules


def test_the_binder_is_idempotent() -> None:
    """Re-running it must register nothing new; every name is already bound to us."""
    assert bind_package("buyer_svc.intent") == ()
    assert bind_package("apps.buyer.svc.src.intent") == ()


def test_the_spelling_list_is_the_two_names_of_this_directory_and_no_more() -> None:
    """This is not a general aliasing facility; widening it would alias unrelated trees."""
    assert SPELLINGS == ("buyer_svc", "apps.buyer.svc.src")
    for root in SPELLINGS:
        module = importlib.import_module(root)
        assert module.__file__ is not None


@pytest.mark.parametrize("ordering", IMPORT_ORDERINGS, ids=range(len(IMPORT_ORDERINGS)))
def test_every_import_ordering_works_in_a_fresh_interpreter(ordering: str) -> None:
    """REGRESSION, and the binder's own foot-gun.

    ``sys.modules`` is consulted for the full dotted name BEFORE any parent is imported.
    The first version of ``_spellings`` registered ``apps.buyer.svc.src.intent`` without
    importing ``apps.buyer.svc.src`` first, so ``import apps.buyer.svc.src.intent as x``
    hit the alias, skipped the parent imports, and died in the attribute walk with
    ``ImportError: cannot import name 'buyer' from 'apps'`` — breaking the exact spelling
    the frozen acceptance suite uses, but ONLY when ``buyer_svc.intent`` was imported
    first. In-process tests could not see it; this one runs each order in its own
    interpreter, which is the only way import order is observable.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"{ordering}; print(p.__name__)"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"{ordering!r} failed:\n{result.stderr}"
