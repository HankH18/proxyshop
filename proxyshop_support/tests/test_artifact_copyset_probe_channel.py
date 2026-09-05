"""The positive control for the artifact checker's RUNTIME half — its report channel.

``test_artifact_copyset.py`` grades whether each deployable image can import every module
its Dockerfile ``COPY``s. That verdict is only worth what the channel carrying it is worth,
and the channel was the weakest part of the file. This module is the guard that keeps the
repair from being quietly undone, and it lives in its own file so the graded suite in
``test_artifact_copyset.py`` keeps its exact shape.

**The defect these tests exist to prevent, stated as it was measured rather than as it was
reasoned about.** The probe subprocess imports every shipped module IN ITS OWN PROCESS —
513 modules across nine images — and used to report back by printing two tagged lines to
stdout: ``SYSPATH <json>`` first, ``RESULT <json>`` last. Stdout is therefore shared with
the code under test. Two independent halves then turned one stray line into a clean image:

* the reader took the FIRST line starting with ``RESULT ``, while the probe emits its own
  LAST — so any shipped module printing such a line at import time replaced the entire
  outcome dictionary; and
* the outcome lookup was ``outcome.get(name, {})``, so a module name the (now-replaced)
  report never mentioned read as a SUCCESSFUL import rather than an ungraded one.

Measured on a scratch copy of this repo at ``d740057``: one genuinely broken module in
``apps/merchant/svc/src/envelope/digest.py`` took ten of the merchant image's modules with
it, including ``merchant_svc.main`` — the ``uvicorn`` entrypoint, i.e. a container that
cannot start — and the checker reported ``10 broken``. Appending one line,
``print('RESULT {"_": {"file": null}}')``, to the unrelated ``merchant_svc.http_limits``
changed that to ``0 broken``: no checker edit, no ``.dockerignore`` edit, no error
anywhere, and every image green. Nothing about that required malice; a debugging ``print``
left behind in any of the 513 shipped modules does it.

**What the repair is, and what these tests actually pin.** Reading the LAST tagged line
would have closed that one instance and left the channel shared. Instead the probe writes
its report to a private file whose name the parent chooses, and ``os.environ.pop``s the
variable naming it before the first shipped module is imported; no line on stdout is parsed
at all. And every name handed to the probe must come back with a verdict, so a short report
is an assertion failure that names the missing modules instead of a clean bill of health.

The first three tests below pin exactly those three properties by EXECUTING them —
:func:`test_stdout_cannot_displace_the_probes_report` reproduces the original poison line
against the real :func:`~proxyshop_support.tests.test_artifact_copyset.run_in_image`, and
the next two truncate and corrupt a real report to prove the reader refuses it. A checker
whose reader defaults an absent measurement to "fine" cannot distinguish a healthy artifact
from an unmeasured one, and that ambiguity is the whole defect class the artifact gates
exist to remove.

**Those three were not enough, and the five after them exist because an adversarial review
MEASURED four ways a shipped module could still zero the report.** Every one of the four
satisfied the pair of conditions the private channel's own docstring named as the defence —
"every name must come back with a verdict and every verdict must grade something" — and
every one of them hid a merchant image that cannot start:

* a fabricated origin on the dotted spelling the origin guard never examined. It inspected
  only names whose first segment is a ``.pkgroot`` package, which skipped 291 of the 914
  verdicts on a clean tree; four appended lines took the merchant image from ``10 broken of
  79`` to ``6 broken of 79`` at exit 0, hiding all four ``envelope/*`` modules.
* a complete forged report followed by ``chmod 444`` on the channel, which killed the
  probe's own final ``_emit`` and left ``0 broken of 79`` at exit 0 —
  ``subprocess.run(check=False)`` with a ``returncode`` nothing branched on.
* ``{"file": None}`` for every verdict, the exact historical poison shape, which passed a
  malformed check that only asked whether one of the two KEYS was present.
* and, in this file rather than in the checker: a stdout reader re-added as a FALLBACK
  survived all three tests above at ``3 passed``, because they pin PRECEDENCE (the channel
  wins when it carries an outcome) and never PRESENCE (that stdout is not read at all).

**Every one of the five new tests was proved red by reverting its own widening**, one at a
time, and the reversion each one detects is named in its docstring. That control is the
only thing that separates a guard test from a comment, and the fourth hole above is what
happens without it.
"""

from __future__ import annotations

import textwrap

import pytest

from proxyshop_support.tests import test_artifact_copyset as copyset

#: The smallest image in the repo (12 shipped modules), so these tests pay for one cheap
#: materialisation rather than the 515-module sweep the graded suite already does.
SMALLEST_IMAGE = "services/shopify-stub/Dockerfile"

#: The cheapest image whose own files are reachable by TWO dotted names — one spelled with a
#: ``.pkgroot`` package (``trust.*``) and one spelled through ``/app`` (``apps.trust.src.*``).
#: :func:`test_a_fabricated_origin_is_refused_on_the_spelling_the_old_guard_skipped` needs
#: that shape and nothing smaller has it: measured on a clean tree, ``shopify_stub`` (12
#: modules) and ``store-agent`` (34) reach every file by ONE name, ``seller-reference`` (24)
#: has 15 names outside ``.pkgroot`` but none of them twinned, and ``apps/trust`` (64
#: modules, 134 names, 32 twinned files) is the smallest that does. Hard-coded rather than
#: derived because deriving it means materialising all nine images to walk their names; the
#: test asserts the shape it needs and fails loudly naming this constant if it ever stops
#: holding, which is the same guarantee at a tenth of the cost.
_IMAGE_WITH_BOTH_SPELLINGS = "apps/trust/Dockerfile"


#: A probe that lies on stdout in exactly the way a shipped module used to be able to, and
#: tells the truth on the private channel. Both tagged prefixes the old reader recognised
#: are emitted, and the ``RESULT`` decoy is emitted BOTH before and after the honest report
#: so the test cannot be satisfied by a reader that merely switched from first-line to
#: last-line. It carries no pip-layer wall: it imports nothing, so there is nothing to wall.
_DECOY_PROBE = (
    textwrap.dedent(
        """
        import json, sys
        """
    )
    + copyset._PROBE_CHANNEL
    + textwrap.dedent(
        """
        print("SYSPATH " + json.dumps(["/decoy/checkout/path"]))
        print('RESULT {"_": {"file": null}}')
        _emit(
            sys_path=["/honest/path"],
            outcome={"honest.module": {"error": "ImportError: the real verdict"}},
        )
        print('RESULT {"_": {"file": null}}')
        """
    )
)


def test_stdout_cannot_displace_the_probes_report() -> None:
    """The exact poison that zeroed the runtime report must now change nothing.

    This is the reproduction, not a paraphrase of it: the decoy string is byte-for-byte the
    one that took the merchant image from ten broken modules to zero, and it runs through
    the real ``run_in_image``. The assertions below deliberately check that the poison WAS
    emitted before checking that it was ignored — a test that only checked the payload would
    also pass if the decoy silently stopped being printed, which is the same "a zero has two
    readings" failure this whole file is about.
    """
    run = copyset.run_in_image(SMALLEST_IMAGE, _DECOY_PROBE)

    assert run.process.returncode == 0, (
        f"the decoy probe did not run, so this test proved nothing:\n{run.diagnosis()}"
    )
    stray = [line for line in run.process.stdout.splitlines() if line.startswith("RESULT ")]
    assert len(stray) == 2, (
        f"the poison lines were never emitted, so a green here would be evidence of nothing. "
        f"stdout was:\n{run.process.stdout}"
    )

    assert run.payload == {
        "sys_path": ["/honest/path"],
        "outcome": {"honest.module": {"error": "ImportError: the real verdict"}},
    }, (
        f"a line written to the probe's stdout reached the graded report. stdout is shared "
        f"with 513 shipped modules; the report must come only from the private channel. "
        f"payload={run.payload!r}"
    )


def _replaying(monkeypatch: pytest.MonkeyPatch, mangle) -> None:
    """Run the real probe, damage the report the way an accident would, and re-serve it."""
    real = copyset.run_in_image

    def replayed(label: str, code: str, *argv: str) -> copyset.ProbeRun:
        run = real(label, code, *argv)
        if run.payload is None or "outcome" not in run.payload:
            return run
        return copyset.ProbeRun(
            run.label, run.process, {**run.payload, "outcome": mangle(run.payload["outcome"])}
        )

    monkeypatch.setattr(copyset, "run_in_image", replayed)


def test_a_module_missing_from_the_report_is_refused_not_read_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name with no verdict is UNGRADED. It used to be indistinguishable from imported.

    The baseline assertion first: this image really is clean today, so the failure the test
    then forces cannot be mistaken for a pre-existing defect. Then one name is dropped from
    an otherwise honest report — the shape any truncation produces, whether from a hijacked
    channel, a probe killed part-way, or a future edit that filters the loop — and the
    reader must refuse the whole report rather than grade the survivors.
    """
    baseline = copyset.image_import_report(SMALLEST_IMAGE)
    assert baseline.broken == {}, (
        f"this test needs a clean image to poison; {SMALLEST_IMAGE} is already red for its "
        f"own reasons:\n{baseline.summary()}"
    )
    assert baseline.module_count > 0, f"{SMALLEST_IMAGE} graded no modules at all"

    def drop_one(outcome: dict) -> dict:
        shortened = dict(outcome)
        shortened.pop(sorted(shortened)[0])
        return shortened

    _replaying(monkeypatch, drop_one)
    copyset.image_import_report.cache_clear()
    try:
        with pytest.raises(AssertionError, match="UNGRADED"):
            copyset.image_import_report(SMALLEST_IMAGE)
    finally:
        copyset.image_import_report.cache_clear()


def test_a_verdict_that_grades_nothing_is_refused_not_read_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry with neither ``error`` nor ``file`` is the same silent pass by another route.

    The broken-module loop keys off ``"error" in outcome[name]``, so an entry saying nothing
    at all counts as an import that worked. Present-but-empty is a different accident from
    absent — a partially written report, a probe that recorded the name before importing it —
    and it has to be as loud.
    """

    def blank_one(outcome: dict) -> dict:
        emptied = dict(outcome)
        emptied[sorted(emptied)[0]] = {}
        return emptied

    _replaying(monkeypatch, blank_one)
    copyset.image_import_report.cache_clear()
    try:
        with pytest.raises(AssertionError, match="grade nothing"):
            copyset.image_import_report(SMALLEST_IMAGE)
    finally:
        copyset.image_import_report.cache_clear()


# =====================================================================================
# The four holes an adversarial review MEASURED after the channel was made private.
#
# Every one of them satisfied the two conditions the channel's own docstring named as the
# defence — "every name must come back with a verdict and every verdict must grade
# something" — and hid a merchant image that cannot start. The four tests below pin the
# four widenings that closed them. Each one is written so that reverting the corresponding
# widening in `test_artifact_copyset.py` turns it RED rather than quiet; that is the only
# property a guard test has, and the fourth of these exists precisely because the three
# above it turned out not to have it.
# =====================================================================================


#: A verdict per asked name, every origin fabricated, delivered by REBINDING the probe's own
#: outcome dictionary from inside a module the probe imports. This is the shape of the
#: measured four-line attack, reduced to a decoy so the test costs one cheap probe instead
#: of a poisoned scratch tree: names lifted from ``sys.argv[1]`` so set-equality passes,
#: every verdict carrying a ``file`` so the malformed check passes, and every origin OUTSIDE
#: the container-shaped tree.
_FABRICATED_ORIGIN_DECOY = (
    textwrap.dedent(
        """
        import json, sys
        """
    )
    + copyset._PROBE_CHANNEL
    + textwrap.dedent(
        """
        _emit(
            sys_path=list(sys.path),
            outcome={n: {"file": "/app/fake.py"} for n in json.loads(sys.argv[1])},
        )
        """
    )
)


def test_an_origin_outside_the_built_tree_is_refused_for_every_name_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The origin guard must examine EVERY verdict, not only first-party top-levels.

    It used to read ``if origin is None or name.split(".")[0] not in packages: continue``,
    with ``packages`` the ``.pkgroot`` names plus ``packages``/``proxyshop_support``. That
    skipped 291 of the 914 verdicts on a clean tree — ``apps.*`` 223, ``services.*`` 52,
    ``fixtures.*`` 16 — and the skipped names are the SECOND dotted name of files that also
    carry a first-party one:

        name=apps.merchant.svc.src.envelope.digest   top=apps          examined=False
        name=merchant_svc.envelope.digest            top=merchant_svc  examined=True

    A file counts as broken only when every one of its names fails, so a fabricated origin
    for the unexamined spelling alone hides it behind its own honest twin. Measured: four
    lines appended to ``apps/merchant/svc/src/http_limits.py`` that rebind
    ``sys.modules["__main__"].outcome`` to
    ``{n: {"file": "/app/fake.py"} for n in json.loads(sys.argv[1])}`` took the merchant
    image from ``10 broken of 79`` to ``6 broken of 79`` at exit 0, hiding all four
    ``envelope/*`` modules including the deliberately poisoned one.

    This runs the fabrication for ALL names rather than only the twinned ones, because the
    property being pinned is that no spelling is exempt. The baseline assertion first: the
    image is clean today, so the failure this test forces cannot be read as a pre-existing
    defect.
    """
    baseline = copyset.image_import_report(SMALLEST_IMAGE)
    assert baseline.broken == {}, (
        f"this test needs a clean image to poison; {SMALLEST_IMAGE} is already red for its "
        f"own reasons:\n{baseline.summary()}"
    )

    monkeypatch.setattr(copyset, "_IMPORT_PROBE", _FABRICATED_ORIGIN_DECOY)
    copyset.image_import_report.cache_clear()
    try:
        with pytest.raises(AssertionError, match="OUTSIDE the"):
            copyset.image_import_report(SMALLEST_IMAGE)
    finally:
        copyset.image_import_report.cache_clear()


def test_a_fabricated_origin_is_refused_on_the_spelling_the_old_guard_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exploitable half of the same hole, on a real report rather than a decoy one.

    :func:`test_an_origin_outside_the_built_tree_is_refused_for_every_name_spelling`
    fabricates every verdict, which the widened guard catches on the first name it looks at
    — including a first-party one the OLD guard also examined. So on its own it does not
    prove the widening happened.

    This one damages ONLY the names whose top-level segment the old guard skipped, and
    leaves every first-party twin honest. That is exactly the measured merchant attack: the
    report stays internally consistent, set-equal, fully graded, and 0-broken, and the old
    guard raised nothing. It is asserted here that the damaged names really are the skipped
    ones and that their twins really were left alone, so a green cannot mean "the mangle did
    nothing".
    """
    label = _IMAGE_WITH_BOTH_SPELLINGS
    first_party = frozenset(copyset.first_party_packages()) | {"packages", "proxyshop_support"}
    damaged: list[str] = []

    def fabricate_the_skipped_spelling(outcome: dict) -> dict:
        forged = dict(outcome)
        for name in sorted(forged):
            if name.split(".")[0] not in first_party:
                forged[name] = {"file": "/app/fake.py"}
                damaged.append(name)
        return forged

    baseline = copyset.image_import_report(label)
    assert baseline.broken == {}, (
        f"this test needs a clean image to poison; {label} is already red for its own "
        f"reasons:\n{baseline.summary()}"
    )

    _replaying(monkeypatch, fabricate_the_skipped_spelling)
    copyset.image_import_report.cache_clear()
    try:
        with pytest.raises(AssertionError, match="OUTSIDE the"):
            copyset.image_import_report(label)
    finally:
        copyset.image_import_report.cache_clear()

    assert damaged, (
        f"{label} has no verdict whose top-level segment is outside {sorted(first_party)}, "
        f"so this test poisoned nothing and its green means nothing. Pick an image that "
        f"reaches its own files by two spellings."
    )
    assert all(name.split(".")[0] not in first_party for name in damaged), (
        f"the mangle damaged a first-party spelling, which the OLD guard already examined, "
        f"so this test would pass without the widening it exists to pin: {damaged[:10]}"
    )


#: A COMPLETE, internally consistent report, followed by ``chmod 444`` on the channel and a
#: second ``_emit`` that therefore dies. The forged report survives on the channel and the
#: subprocess exits non-zero — which is the whole of the hole: ``subprocess.run(check=False)``
#: and a ``returncode`` that was read in exactly one place, ``ProbeRun.diagnosis()``, i.e.
#: assertion prose and never a branch.
_CHMOD_THE_CHANNEL_DECOY = (
    textwrap.dedent(
        """
        import json, os, sys
        """
    )
    + copyset._PROBE_CHANNEL
    + textwrap.dedent(
        """
        _honest = list(sys.path)
        _emit(
            sys_path=_honest,
            outcome={"forged.module": {"file": os.path.join(os.getcwd(), "forged.py")}},
        )
        os.chmod(_CHANNEL, 0o444)
        _emit(
            sys_path=_honest,
            outcome={"honest.module": {"error": "ImportError: the real verdict"}},
        )
        """
    )
)


def test_a_probe_that_exits_non_zero_is_refused_however_complete_its_report() -> None:
    """Nothing the probe left behind may be read before its exit status is.

    Measured on a scratch copy of this tree: a shipped module that globs
    ``/private/var/folders/*/*/T/copyset-probe-*.json``, writes a complete report whose
    origins are ``os.path.join(os.getcwd(), "fake.py")`` — cwd IS the container-shaped tree,
    so the origins validate — and then ``os.chmod(p, 0o444)`` killed the probe's own final
    ``_emit`` with ``PermissionError: [Errno 13] Permission denied`` while the gate reported
    ``0 broken of 79`` at exit 0, with the deliberately poisoned module broken throughout.

    The three assertions are ordered so a green cannot be evidence of nothing: the report on
    the channel really was complete and honest-looking (so the refusal is not about a
    malformed payload), the probe really did die the way the attack makes it die, and the
    refusal names the exit status. ``apps/trust/tests/test_repro_open_tickets.py:492``
    already asserted this about its own probe; the precedent was simply not followed here.
    """
    with pytest.raises(AssertionError) as caught:
        copyset.run_in_image(SMALLEST_IMAGE, _CHMOD_THE_CHANNEL_DECOY)
    message = str(caught.value)

    assert "forged.module" in message, (
        f"the forged report did not survive on the channel, so this test did not reproduce "
        f"the attack it exists to refuse — a refusal here would be about a missing payload "
        f"rather than about the exit status:\n{message}"
    )
    assert "PermissionError" in message, (
        f"the probe did not die of the chmod, so the non-zero exit this test pins was "
        f"produced by something other than the measured attack:\n{message}"
    )
    assert "exited 1" in message, (
        f"the probe's exit status is not what the refusal is about, so a future edit that "
        f"drops the returncode check could leave this test green:\n{message}"
    )


def test_every_verdict_reduced_to_a_null_file_is_refused_not_read_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``{"file": None}`` — the historical poison shape — used to pass every check.

    ``print('RESULT {"_": {"file": null}}')`` is the literal line that took the merchant
    image from ten broken modules to zero when stdout was the channel. The channel is
    private now, but the SHAPE still went straight through: ``{"file": None}`` passed
    set-equality, passed a malformed check that only asked whether one of the two KEYS was
    present, and was then skipped by the origin guard on ``origin is None``. Measured: a
    shipped module that rebinds ``__main__._emit`` and writes every verdict as
    ``{"file": None}`` reported ``0 broken of 79`` at exit 0 with the poisoned module still
    broken.

    On a clean tree across all nine images there are 914 verdicts and ZERO with a null or
    empty ``file``, so requiring a non-empty origin string costs nothing.

    **The exact ``match`` phrase is load-bearing, and not for tidiness.** Two widenings now
    refuse this shape and they overlap: reverting the malformed check ALONE still leaves the
    null-file report red, because the widened origin guard then reports 12 verdicts whose
    origin is ``None`` as being outside the tree. Measured — that is what this test's
    reversion control produced. So an assertion that accepted any ``AssertionError`` would
    stay green while the check it exists to pin was gone, and would only turn red once BOTH
    widenings had been reverted. Matching the malformed check's own wording is what makes
    this test about the malformed check.
    """

    def null_every_origin(outcome: dict) -> dict:
        return {name: {"file": None} for name in outcome}

    _replaying(monkeypatch, null_every_origin)
    copyset.image_import_report.cache_clear()
    try:
        with pytest.raises(AssertionError, match="grade nothing"):
            copyset.image_import_report(SMALLEST_IMAGE)
    finally:
        copyset.image_import_report.cache_clear()


#: A probe that writes ``sys_path`` and then dies before it ever writes an ``outcome``,
#: while a complete, plausible ``RESULT `` line sits on stdout. This is a REACHABLE state,
#: not a contrivance: the real probe's first ``_emit`` records ``sys_path`` alone, so any
#: shipped module that kills the process mid-import leaves exactly this payload — and it
#: exits 0 here so that no other check can be what refuses it. The forged origins are built
#: from ``os.getcwd()``, which IS the container-shaped tree, so they would pass the origin
#: guard too: the ONLY thing standing between this and a clean report is that stdout is
#: never read.
_STDOUT_FALLBACK_DECOY = (
    textwrap.dedent(
        """
        import json, os, sys
        """
    )
    + copyset._PROBE_CHANNEL
    + textwrap.dedent(
        """
        _emit(sys_path=list(sys.path))
        print(
            "RESULT "
            + json.dumps(
                {
                    n: {"file": os.path.join(os.getcwd(), "forged.py")}
                    for n in json.loads(sys.argv[1])
                }
            ),
            flush=True,
        )
        """
    )
)


def test_stdout_is_not_read_even_as_a_fallback_when_the_channel_has_no_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """:func:`test_stdout_cannot_displace_the_probes_report` pins PRECEDENCE, not PRESENCE.

    Measured gap: re-adding the stdout ``RESULT `` reader to ``run_in_image`` guarded as a
    FALLBACK —

        if payload is None or "outcome" not in payload:
            for line in process.stdout.splitlines():
                if line.startswith("RESULT "):
                    payload = {**(payload or {}), "outcome": json.loads(line[7:])}

    — left the three tests above at ``3 passed``. Unguarded (preferred over the channel) is
    correctly caught by the precedence test, so the suite could tell a reader that WINS from
    one that does not exist, and not a reader that merely waits its turn.

    The reachable shape is the one below: the probe's first ``_emit`` writes ``sys_path``
    alone, so a payload with no ``outcome`` and a returncode of 0 is what a module that
    kills the process mid-import leaves behind — while a flushed ``print`` sits on stdout
    with a complete report whose origins are inside the built tree and would therefore pass
    every other check. With the fallback reader present that report is graded and the image
    reads clean; without it the reader refuses a payload that never finished.

    The assertions after the refusal are what stop this from being satisfied by a decoy that
    silently stopped emitting: the poison really was on stdout, the probe really did exit 0,
    and the channel really did carry ``sys_path`` and no ``outcome``.
    """
    seen: list[copyset.ProbeRun] = []
    real = copyset.run_in_image

    def recording(label: str, code: str, *argv: str) -> copyset.ProbeRun:
        run = real(label, code, *argv)
        seen.append(run)
        return run

    monkeypatch.setattr(copyset, "_IMPORT_PROBE", _STDOUT_FALLBACK_DECOY)
    monkeypatch.setattr(copyset, "run_in_image", recording)
    copyset.image_import_report.cache_clear()
    try:
        with pytest.raises(AssertionError, match="did not finish"):
            copyset.image_import_report(SMALLEST_IMAGE)
    finally:
        copyset.image_import_report.cache_clear()

    assert len(seen) == 1, f"expected exactly one probe run, recorded {len(seen)}"
    run = seen[0]
    poison = [line for line in run.process.stdout.splitlines() if line.startswith("RESULT ")]
    assert len(poison) == 1 and "forged.py" in poison[0], (
        f"the decoy never wrote a complete report to stdout, so a green here would be "
        f"evidence of nothing. stdout was:\n{run.process.stdout[:800]}"
    )
    assert run.process.returncode == 0, (
        f"the decoy exited {run.process.returncode}, so the returncode check could be what "
        f"refused it rather than the refusal to read stdout:\n{run.diagnosis()}"
    )
    assert run.payload is not None and set(run.payload) == {"sys_path"}, (
        f"the channel did not carry the started-but-unfinished payload this test is about; "
        f"it carried {run.payload!r}"
    )
