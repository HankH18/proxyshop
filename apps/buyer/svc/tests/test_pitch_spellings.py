"""One module object per file under BOTH dotted spellings of ``apps/buyer/svc/src/pitch``.

This tree is importable twice — ``buyer_svc.pitch`` through the tracked ``.pkgroot/buyer_svc``
symlink, and ``apps.buyer.svc.src.pitch``, the repo-root path the frozen acceptance suite uses.
Left alone, Python EXECUTES every file a second time under the second name, and the two
spellings then disagree about object identity.

For this package that is not cosmetic, and the reason is the writer seam.
``buyer_svc.pitch.writer`` holds ``_override`` at module scope — the ONE way a deployment or a
test installs the client the buyer-side agent writes with. Two module objects means two
``_override`` variables, so a writer installed through one spelling would be invisible to the
render path reached through the other: the render would silently fall back to the deterministic
assembly with every assertion still green, which is precisely the shape of bug that makes a
model seam look wired when it is not.

``buyer_svc.pitch.__init__`` calls :func:`buyer_svc.intent._spellings.bind_package` for that,
reusing the implementation rather than making a fourth copy of it (``intent``, ``accept`` and
``packages/llm`` carry the other three). This file is the check that the call actually took.
"""

from __future__ import annotations

import importlib


def test_both_spellings_of_the_package_are_one_module_object():
    first = importlib.import_module("buyer_svc.pitch")
    second = importlib.import_module("apps.buyer.svc.src.pitch")
    assert first is second


def test_both_spellings_of_every_submodule_are_one_module_object():
    for leaf in ("material", "writing", "writer"):
        first = importlib.import_module(f"buyer_svc.pitch.{leaf}")
        second = importlib.import_module(f"apps.buyer.svc.src.pitch.{leaf}")
        assert first is second, leaf


def test_the_writer_seam_is_one_variable_under_both_spellings():
    """The property that actually matters: an installed writer is visible from either name."""
    here = importlib.import_module("buyer_svc.pitch.writer")
    there = importlib.import_module("apps.buyer.svc.src.pitch.writer")
    sentinel = object()
    here.set_pitch_writer(sentinel)
    try:
        assert there.pitch_writer() is sentinel
    finally:
        here.set_pitch_writer(None)
    assert there.pitch_writer() is not sentinel


def test_one_slot_pitch_class_so_an_isinstance_check_cannot_split():
    first = importlib.import_module("buyer_svc.pitch")
    second = importlib.import_module("apps.buyer.svc.src.pitch")
    assert first.SlotPitch is second.SlotPitch
    assert first.PLATFORM_CONTRACT is second.PLATFORM_CONTRACT
