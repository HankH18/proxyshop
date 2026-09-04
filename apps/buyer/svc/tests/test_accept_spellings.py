"""One module object per file, across both dotted spellings of this tree (T-072).

``apps/buyer/svc/src`` is importable as ``buyer_svc.<mod>`` (through the tracked
``.pkgroot/buyer_svc`` symlink) and as ``apps.buyer.svc.src.<mod>`` (the repo-root path the
frozen acceptance suite uses). Python will happily execute every file TWICE, once per
spelling, and the duplication is invisible until something in the package holds identity or
state — which this one does, in both forms:

* ``OffDomainPermalink`` raised through one spelling is not caught by an ``except`` written
  against the other, so the route module's 502 branch would miss the refusal it exists for;
* the accept ledger exists twice, so "one auction gets one checkout" holds only per
  spelling — and a pytest session running the frozen acceptance suite beside the FastAPI
  app is exactly a process that reaches ``accept`` both ways.

Measured on this worktree before ``_spellings.bind_package`` was added to this package:
``apps.buyer.svc.src.accept is buyer_svc.accept`` was ``False``.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import buyer_svc.accept as pkgroot_spelling
import pytest

import apps.buyer.svc.src.accept as repo_root_spelling
from apps.buyer.svc.src.accept._spellings import SPELLINGS, bind_spellings

SUBMODULES = ("_reading", "errors", "handoff", "labels", "permalink")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: Every order in which the two spellings can arrive. Each runs in a FRESH interpreter,
#: because import order is process state and an in-process test can only ever observe the
#: one order this session happened to take.
IMPORT_ORDERINGS = (
    "import buyer_svc.accept; import apps.buyer.svc.src.accept as p",
    "import apps.buyer.svc.src.accept; import buyer_svc.accept as p",
    "import buyer_svc.accept.routes; import apps.buyer.svc.src.accept.routes as p",
    "import apps.buyer.svc.src.accept.routes; import buyer_svc.accept.routes as p",
    "import buyer_svc.main; import apps.buyer.svc.src.accept as p",
    "from apps.buyer.svc.src.accept import accept; import buyer_svc.accept as p",
)


def test_both_spellings_are_the_same_package_object() -> None:
    assert repo_root_spelling is pkgroot_spelling


@pytest.mark.parametrize("name", SUBMODULES)
def test_both_spellings_are_the_same_submodule_object(name) -> None:
    assert getattr(repo_root_spelling, name) is getattr(pkgroot_spelling, name)


def test_a_refusal_raised_through_one_spelling_is_caught_through_the_other() -> None:
    """The failure this file exists for: two classes, same line, neither catching the other."""
    with pytest.raises(pkgroot_spelling.OffDomainPermalink):
        repo_root_spelling.verify_permalink("https://evil.example/c", "store-x.example.com")
    with pytest.raises(repo_root_spelling.AcceptError):
        pkgroot_spelling.verify_permalink("javascript:alert(1)")


def test_there_is_exactly_one_accept_ledger() -> None:
    """Two ledgers would make "one auction gets one checkout" true only per spelling."""
    assert repo_root_spelling.accepted() is pkgroot_spelling.accepted()

    exchange = _Exchange()
    repo_root_spelling.reset_accepted()
    try:
        slot = {"slot": "fit", "bid_ref": "bid-1", "auction_id": "auc-spell-1"}
        pkgroot_spelling.accept(slot, exchange)
        with pytest.raises(repo_root_spelling.OfferAlreadyAccepted):
            repo_root_spelling.accept(slot, exchange)
        assert len(exchange.calls) == 1
    finally:
        repo_root_spelling.reset_accepted()


class _Exchange:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def accept_offer(self, payload):
        self.calls.append(payload)
        return {"permalink_url": "https://store-x.example.com/cart/1:1?discount=PS-A"}


@pytest.mark.parametrize("program", IMPORT_ORDERINGS)
def test_identity_holds_whichever_spelling_is_imported_first(program) -> None:
    """In-process this session took ONE order; these take the other five, freshly."""
    script = (
        f"{program}\n"
        "import apps.buyer.svc.src.accept as a, buyer_svc.accept as b\n"
        "assert a is b, 'the two spellings are different module objects'\n"
        "assert a.OffDomainPermalink is b.OffDomainPermalink\n"
        "assert a.accepted() is b.accepted()\n"
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
