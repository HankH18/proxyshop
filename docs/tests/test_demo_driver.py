"""Does the demo runbook's section-3 command actually produce a purchase?

``test_runbook_executability.py`` next door grades **resolvability** — every command the
runbook tells an operator to type names something that exists and that pytest can reach. Its
own docstring is explicit that this is deliberately not semantics: ``docs/demo/e2e_live.sh``
containing only ``exit 0`` resolves, and that gate passes it.

This file grades the other half for the one command the live demo beat now names. It runs
``proxyshop_demo`` and asks whether the journey it printed really happened: a real single-use
discount code, a non-empty shortlist, a blacklisted store denied before anybody was asked, a
silent store represented at its catalogue list price, and an order closed at the merchant.

**Why that is worth a test rather than a manual look.** A demo driver is the one artifact in a
repository whose failure mode is silent: nobody runs it between demos, and the first person to
notice it broke is standing in front of an audience. It is also the artifact most able to
*look* fine while proving nothing — a driver whose services stopped answering could still print
every heading, every paragraph of prose and a tidy summary, and the only difference would be
numbers nobody was diffing. So the assertions here are about the DATA the run produced, never
about the headings around it.

The gaps are asserted too, and not because a gap is a good thing. The driver's contract is that
a beat the product cannot perform yet is printed as a ``DOES NOT RUN YET`` block rather than
skipped, and a driver that quietly dropped those blocks would read as a system that works. If a
gap closes, the count changes and the assertion here is the thing that says so out loud — see
:func:`test_every_beat_that_does_not_run_is_reported_rather_than_skipped`, which pins the
partition (every gap recorded is a gap printed) and not the number.
"""

from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The environment this driver writes, because it configures the services it starts the way a
#: deployment configures them. Restored around the in-process run: a test that left
#: ``EXCHANGE_DEPLOYMENT`` pointing into a deleted temp directory would change what every later
#: test in the session reads, and the failure would land on somebody else's file.
DRIVER_ENV_KEYS = (
    "STORE_AGENT_CONTEXT",
    "STORE_AGENT_STORE_DOMAIN",
    "EXCHANGE_DEPLOYMENT",
    "EXCHANGE_DEPLOYMENT_JSON",
)

#: The published shape of a minted code: ``PSX-`` and eight Crockford base32 characters
#: (``I``, ``L``, ``O`` and ``U`` are excluded from that alphabet so a human reading one aloud
#: cannot turn it into a different code).
CODE_PATTERN = re.compile(r"^PSX-[0-9A-HJKMNP-TV-Z]{8}$")

#: One record as ``proxyshop_support.logging_config``'s text formatter writes it:
#: ``%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s`` with
#: ``datefmt="%Y-%m-%dT%H:%M:%S%z"``. Anchored at the start of a line on purpose — the anchor
#: is what separates "a new record" from "the second line of the record above", and a
#: traceback is entirely lines of the second kind.
STDERR_RECORD = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} (?P<level>[A-Z]+) +\S+ \[[^\]]*\] "
)

#: Text that means something failed, whatever the exit status said. ``was never retrieved`` is
#: asyncio's, for a task whose exception nobody awaited — the canonical shape of a failure that
#: leaves the process code at zero.
STDERR_FAILURE_SIGNATURES = (
    "Traceback (most recent call last)",
    "DEMO FAILED",
    "Exception in thread",
    "Exception ignored",
    "--- Logging error ---",
    "was never retrieved",
)


def stderr_faults(stderr: str) -> tuple[list[str], list[str]]:
    """The two kinds of line ``python -m proxyshop_demo`` must never write to stderr.

    Returns ``(unattributed, loud)``:

    * **unattributed** — a line that is neither a log record nor an indented continuation of
      one. Everything ``logging`` emits carries the header above; everything that does not is
      something writing to the stream directly, which is a traceback, a stray ``print``, a
      thread dying, or the driver's own ``DEMO FAILED:``.
    * **loud** — a record at ``ERROR`` or ``CRITICAL``. A level name the stdlib does not know
      counts as loud too, rather than being waved through as "probably fine".
    """
    unattributed: list[str] = []
    loud: list[str] = []
    started = False
    for line in stderr.splitlines():
        match = STDERR_RECORD.match(line)
        if match:
            started = True
            level = logging.getLevelName(match.group("level"))
            if not isinstance(level, int) or level >= logging.ERROR:
                loud.append(line)
        elif line.strip() and not (started and line[:1].isspace()):
            unattributed.append(line)
    return unattributed, loud


#: The four sellers the S1 run fixture puts on the roster, and what each is here to prove.
BLACKLISTED_STORE = "store-blocked"
SILENT_STORE = "store-slowreply"
WINNING_STORE = "store-northroast"
SILENT_STORE_LIST_PRICE = 519.0


@pytest.fixture(scope="module")
def journey() -> Iterator[tuple[Any, str]]:
    """One in-process run of the driver, with every process-wide seam it touches restored.

    Module-scoped because the run takes about eight seconds of real sockets and there is
    nothing a second one would establish that the first did not.

    Two things are put back afterwards, and both were learned the hard way in this repository
    rather than guessed:

    * the four environment variables above, because the driver writes them into files under a
      temporary directory that is gone by the time anything else could read them;
    * ``exchange.accept.offer``'s registered-domain seam, which is MODULE level and process
      wide. The composition root reaches it through ``configure_accept``, so a file that runs a
      deployment and does not restore it changes what every later test in the process reads.
      Same fixture, same reason, as ``apps/exchange/tests/test_composition_root.py``'s.
    """
    from exchange.accept.offer import use_registered_domains

    from proxyshop_demo.s1 import run_journey

    saved = {key: os.environ.get(key) for key in DRIVER_ENV_KEYS}
    previous_domains = use_registered_domains(None)
    buffer = io.StringIO()
    try:
        result = run_journey(stream=buffer)
        yield result, buffer.getvalue()
    finally:
        use_registered_domains(previous_domains)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# =====================================================================================
# The acceptance criteria: a real code, and a real shortlist
# =====================================================================================
def test_the_driver_minted_a_real_single_use_code(journey: tuple[Any, str]) -> None:
    """The run ends with a code of the published shape, reachable through the permalink.

    The shape check is half of it. The other half is that the code is IN the permalink: a
    driver that minted a code and built a link to something else would satisfy a `startswith`
    on either one alone, and the shopper would follow a URL that redeems nothing.
    """
    result, _ = journey
    assert CODE_PATTERN.match(result.code), (
        f"the accepted offer produced {result.code!r}, which is not a minted single-use code"
    )
    assert result.permalink_url.startswith(
        f"https://{result.accepted_store_id}.example.com/cart/"
    ), (
        f"the permalink {result.permalink_url!r} is not on the domain the platform registered "
        f"for {result.accepted_store_id!r}; an off-domain checkout is the C10/D22 failure this "
        f"run is meant to show cannot happen"
    )
    assert f"discount={result.code}" in result.permalink_url, (
        f"the permalink {result.permalink_url!r} does not carry the code {result.code!r} that "
        f"was minted for it"
    )


def test_the_driver_showed_a_non_empty_shortlist(journey: tuple[Any, str]) -> None:
    """The shopper is shown slots, each carrying the four things a slot has to carry."""
    result, _ = journey
    assert result.shortlist_slots, (
        f"the shortlist was empty. Every candidate was excluded: "
        f"{[(row['store_id'], row['exclusion_reasons']) for row in result.excluded]}"
    )
    for slot in result.shortlist_slots:
        assert slot["bid_ref"], f"a shortlist slot with no bid_ref cannot be accepted: {slot}"
        assert slot["slot"], f"a slot with no name tells a shopper nothing: {slot}"
        assert isinstance(slot["fit_score"], (int, float)), slot
        assert slot["trust_summary"].get("store_id"), (
            f"a slot whose trust summary names no store is claiming a trust story it cannot "
            f"show: {slot}"
        )
    assert result.accepted_bid_ref == result.shortlist_slots[0]["bid_ref"], (
        "the run accepted something other than the slot it printed as the top of the shortlist"
    )


def test_the_ranking_really_ordered_the_bids(journey: tuple[Any, str]) -> None:
    """More than one candidate was ranked, and the order is the score's.

    A one-candidate ranking is a ranking that proved nothing: it is in order by construction,
    and the weighted formula could be returning a constant.
    """
    result, _ = journey
    assert len(result.ranked) >= 2, f"only {len(result.ranked)} candidate(s) reached the ranker"
    scores = [row["rank_score"] for row in result.ranked]
    assert scores == sorted(scores, reverse=True), f"ranked is not in score order: {scores}"
    assert len(set(scores)) > 1, (
        f"every ranked candidate scored the same ({scores}), so the shortlist's order is "
        f"position rather than merit and this run demonstrates no ranking at all"
    )
    for row in result.ranked:
        assert row["components"], f"a ranked bid with no component breakdown: {row}"
        assert abs(sum(row["components"].values()) - row["rank_score"]) < 1e-9, (
            f"the published components do not sum to the score they explain: {row}"
        )


# =====================================================================================
# The gates the starting slice exists to demonstrate
# =====================================================================================
def test_the_blacklisted_store_was_denied_before_anybody_was_asked(
    journey: tuple[Any, str],
) -> None:
    """S8: never asked, never collected, never ranked, never shown.

    All four, checked separately. "It is not on the shortlist" is the weakest of the four and
    the only one a broken gate would still satisfy by accident.
    """
    result, _ = journey
    denied = {row["store_id"]: row for row in result.denied}
    assert BLACKLISTED_STORE in denied, (
        f"{BLACKLISTED_STORE} was not denied at the eligibility gate; denied={result.denied}"
    )
    assert denied[BLACKLISTED_STORE]["status"] == "blacklisted", denied[BLACKLISTED_STORE]
    assert denied[BLACKLISTED_STORE]["reason"], "a denial with no reason to show an operator"

    assert BLACKLISTED_STORE not in result.solicited, "the blacklisted store was asked to bid"
    assert BLACKLISTED_STORE not in [entry["store_id"] for entry in result.entries], (
        "the blacklisted store was collected into the auction's entries"
    )
    assert BLACKLISTED_STORE not in [row["store_id"] for row in result.ranked], (
        "the blacklisted store was ranked"
    )
    assert not any(BLACKLISTED_STORE in slot["bid_ref"] for slot in result.shortlist_slots), (
        "the blacklisted store reached the shopper's shortlist"
    )


def test_the_silent_store_was_represented_at_its_catalogue_list_price(
    journey: tuple[Any, str],
) -> None:
    """R10: a store that never answered still has an entry, and it says why.

    The price is asserted, not merely the flag. A fallback at 0.00 is also a "fallback", and it
    is the shape a missing list price mints — which would be a free item rather than a
    represented store.
    """
    result, _ = journey
    entry = result.bid_of(SILENT_STORE)
    assert entry is not None, f"{SILENT_STORE} has no entry at all; entries={result.entries}"
    assert entry["fallback"] is True, f"the silent store was collected as a real bid: {entry}"
    assert entry["fallback_reason"] == "no_response", entry
    assert entry["unit_price"] == pytest.approx(SILENT_STORE_LIST_PRICE), (
        f"the fallback was minted at {entry['unit_price']}, not the roster's list price "
        f"{SILENT_STORE_LIST_PRICE}"
    )


def test_the_hosted_stores_really_answered_over_http(journey: tuple[Any, str]) -> None:
    """The two hosted agents produced real bids, not the exchange's own fallback.

    This is the assertion that makes the whole run mean something. Every other number here is
    also produced by a run in which no agent was ever reached: a fallback entry has a price, a
    fallback can be ranked in principle, and the narrative reads identically. What separates
    the two is ``fallback: false`` on an entry whose price came off a store's own catalogue.
    """
    result, _ = journey
    real = [entry for entry in result.entries if not entry["fallback"]]
    assert len(real) >= 2, (
        f"only {len(real)} store(s) answered a solicitation; the rest were represented by the "
        f"exchange's own list-price fallback, which means the HTTP hop did not happen: "
        f"{result.entries}"
    )
    assert result.accepted_store_id in {entry["store_id"] for entry in real}, (
        f"the accepted offer came from {result.accepted_store_id!r}, which did not bid"
    )
    assert result.agent_urls, "no store agent was served at all"


def test_the_merchant_closed_a_real_order_and_verified_a_signed_webhook(
    journey: tuple[Any, str],
) -> None:
    """The downstream half: an order at the merchant, and an HMAC delivery it accepted."""
    result, _ = journey
    assert result.order_name.startswith("#"), (
        f"the merchant closed no order; order_name={result.order_name!r}"
    )
    assert float(result.order_total) == pytest.approx(389.0), (
        f"the order was closed for {result.order_total!r}, not the winning offer's price"
    )
    assert result.webhook_ledger_kind == "order_paid", (
        f"the signed orders/paid delivery was not read as a paid order: "
        f"{result.webhook_ledger_kind!r}"
    )
    assert result.pixel_order_ref, "the web pixel's beacon never reached the merchant collector"
    assert result.second_use_honoured is False, (
        "the single-use discount code was honoured by a second order"
    )


# =====================================================================================
# The honesty contract
# =====================================================================================
def test_every_beat_that_does_not_run_is_reported_rather_than_skipped(
    journey: tuple[Any, str],
) -> None:
    """A gap the driver recorded is a gap the driver PRINTED. The partition, not the count.

    Pinning the number would make closing a gap a test failure, which is backwards. What is
    pinned is that the two lists agree: a driver that recorded a gap and rendered nothing, or
    rendered a block it did not record, has a hole exactly where its credibility lives.
    """
    result, output = journey
    assert output.count("DOES NOT RUN YET") == len(result.gaps), (
        f"the driver recorded {len(result.gaps)} gap(s) and printed "
        f"{output.count('DOES NOT RUN YET')} block(s); a gap that is recorded and not printed "
        f"is a beat the audience never learns is missing"
    )
    for headline in result.gaps:
        assert headline in output, f"the gap {headline!r} was recorded but never rendered"


def test_the_narrative_carries_the_real_values_and_not_only_the_headings(
    journey: tuple[Any, str],
) -> None:
    """The prose is worthless if the numbers beside it are not this run's."""
    result, output = journey
    for needle in (
        result.code,
        result.permalink_url,
        result.auction_id,
        result.order_name,
        result.accepted_store_id,
        SILENT_STORE,
        BLACKLISTED_STORE,
    ):
        assert needle in output, f"{needle!r} is part of this run and never reached the output"


# =====================================================================================
# The command an operator actually types
# =====================================================================================
@pytest.mark.timeout(240)
def test_the_command_the_runbook_names_runs_and_exits_zero() -> None:
    """``python -m proxyshop_demo``, as a subprocess, exactly as section 3 tells you to type it.

    Separate from every test above, and deliberately not a duplicate of them. Everything above
    imports :func:`run_journey` and would keep passing if ``__main__.py`` were deleted, if the
    package could not be run with ``-m``, or if the driver exited non-zero after printing a
    perfectly good journey. This is the one test that grades the artifact the runbook hands an
    operator.

    ``stderr`` is graded rather than ignored, and what is graded is the SHAPE of what lands
    there. The purpose is the one this test was written around and is unchanged: *the driver's
    own failure path writes there, and a demo that printed a traceback under a green exit
    status is the shape of problem this whole file exists to catch.*

    **What changed is the proxy, and only because the premise under it was deliberately
    removed.** Until T-308 this could be spelled ``stderr == ""``, because nothing in the
    repository configured logging at all: ``proxyshop_support/logging_config.py``'s header
    records the measurement — a repo-wide grep for
    ``basicConfig|dictConfig|FileHandler|structlog|logging.config`` returned zero hits, so a
    started service had ``root.handlers == []`` at level ``WARNING`` and every ``_log.info``
    was dropped before a record was constructed. T-308 ended that on purpose: every
    ``create_app()`` now calls ``configure_logging()``, which delivers to stderr, and this
    driver starts five of them inside one process. "Nothing legitimate writes to stderr" is
    now false BY DESIGN, so an empty-stderr assertion no longer grades tracebacks — it grades
    whether T-308 happened, which is a different (and already tested) thing.

    So the three assertions below say what that sentence actually means. Each is narrower than
    "no output", not looser than it:

    * **nothing unattributed** — every line is a log record or an indented continuation of
      one. ``Traceback (most recent call last):``, a bare ``print(..., file=sys.stderr)``,
      threading's ``Exception in thread ...`` and the driver's own ``DEMO FAILED:`` are all
      unindented and match no record header, so all four still fail here. This is the
      assertion that keeps the original's reach: a "contains no traceback" check would have
      let the last three through.
    * **nothing loud** — no record at ``ERROR`` or ``CRITICAL``. asyncio's "Task exception was
      never retrieved", which is precisely an exception swallowed under a green exit, is an
      ``ERROR`` record and lands here.
    * **no failure signature in the raw text**, so a failure is named by shape in the message
      rather than only by the line that carried it.

    A ``WARNING`` is deliberately allowed, and that is not a loophole. This driver's whole
    contract is that it REPORTS what is degraded instead of hiding it — see
    :func:`test_every_beat_that_does_not_run_is_reported_rather_than_skipped` — so asserting
    that the demo never warns would be asserting that the demo is never degraded, which is the
    opposite of what every other test in this file grades.

    A run today emits **none**, and how that happened is the part worth writing down, because
    the "no loud record" assertion above is now doing real work rather than passing vacuously.

    T-150 shipped a once-per-sink ``WARNING`` saying the trust service at ``http://trust:8084``
    was unreachable and the auction's transitions were landing in no chained ledger — which was
    the honest truth about a demo running against a service nobody had started.
    ``proxyshop_support.trust_ledger`` has since replaced that with one ``ERROR`` the moment
    delivery stops and one ``INFO`` the moment it resumes, on the argument that ``WARNING`` is
    the wrong level for a required write that is not happening.

    That change moved the line **into** the "nothing loud" assertion. So this test is now a
    live check that the demo's audit trail actually lands: the driver serves
    ``trust.main:create_app()`` on a loopback port and states that address as the exchange's
    ``trust_url``, and if that ever stops working the ``ERROR`` fires and this test goes red.
    Nothing here silences it — no level was lowered and no stream redirected; the same record
    still fires on the same code path for anyone who runs the exchange with no trust service
    reachable, which is exactly what a probe pointing the driver at a dead port produces.
    """
    environ = {key: value for key, value in os.environ.items() if key not in DRIVER_ENV_KEYS}
    completed = subprocess.run(
        [sys.executable, "-m", "proxyshop_demo"],
        cwd=REPO_ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (
        f"`python -m proxyshop_demo` exited {completed.returncode}\n"
        f"--- stderr ---\n{completed.stderr}\n--- last of stdout ---\n{completed.stdout[-3000:]}"
    )
    unattributed, loud = stderr_faults(completed.stderr)
    assert not unattributed, (
        f"{len(unattributed)} line(s) on the demo's stderr belong to no log record. A "
        f"traceback, a stray print, a dead thread or the driver's own failure path is what "
        f"that looks like, and a green exit status did not notice any of them:\n"
        + "\n".join(unattributed[:20])
        + f"\n--- all of stderr ---\n{completed.stderr}"
    )
    assert not loud, (
        f"the demo logged {len(loud)} record(s) at ERROR or worse and still exited 0:\n"
        + "\n".join(loud[:20])
    )
    signatures = [text for text in STDERR_FAILURE_SIGNATURES if text in completed.stderr]
    assert not signatures, (
        f"the demo's stderr carries {signatures}, which is a failure wearing a zero exit "
        f"status:\n{completed.stderr}"
    )

    codes = re.findall(r"PSX-[0-9A-HJKMNP-TV-Z]{8}", completed.stdout)
    assert codes, (
        f"the command printed no minted discount code, so the journey did not complete:\n"
        f"{completed.stdout[-3000:]}"
    )
    assert len(set(codes)) == 1, (
        f"the run printed more than one distinct code ({sorted(set(codes))}); a single "
        f"acceptance mints exactly one"
    )
    for needle in (
        "BEAT 1.",
        "BEAT 6.",
        "/buyer/intent/clarify",
        "/v1/bid-requests",
        "/auctions",
        WINNING_STORE,
        "DOES NOT RUN YET",
        "WHAT JUST RAN, AND WHAT DID NOT",
    ):
        assert needle in completed.stdout, (
            f"{needle!r} is missing from the demo's output; the journey it printed is not the "
            f"one this command is supposed to walk"
        )
