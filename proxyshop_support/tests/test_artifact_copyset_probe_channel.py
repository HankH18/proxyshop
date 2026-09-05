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

The three tests below pin exactly those three properties by EXECUTING them —
:func:`test_stdout_cannot_displace_the_probes_report` reproduces the original poison line
against the real :func:`~proxyshop_support.tests.test_artifact_copyset.run_in_image`, and
the other two truncate and corrupt a real report to prove the reader refuses it. A checker
whose reader defaults an absent measurement to "fine" cannot distinguish a healthy artifact
from an unmeasured one, and that ambiguity is the whole defect class the artifact gates
exist to remove.
"""

from __future__ import annotations

import textwrap

import pytest

from proxyshop_support.tests import test_artifact_copyset as copyset

#: The smallest image in the repo (12 shipped modules), so these tests pay for one cheap
#: materialisation rather than the 513-module sweep the graded suite already does.
SMALLEST_IMAGE = "services/shopify-stub/Dockerfile"


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
