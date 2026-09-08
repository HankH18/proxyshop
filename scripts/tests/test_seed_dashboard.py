"""The checked-in merchant seed panel: does it load, is it marked, and can it be laundered?

Four groups, and they are the four claims `deploy/demo-seed/README.md` makes:

1. **it loads, and only if its bytes match its own record** — including the case that matters
   most, a record with the digest *removed*. A missing digest is not a lesser failure than a
   wrong one; it is the same failure with the evidence deleted, and it is the cheapest way to
   launder an edit.
2. **it is the shape the merchant dashboard actually serves** — all six panels, all six trust
   dimensions, the four closed loss reasons and no fifth, no `wins` field anywhere.
3. **every manufactured row is marked** — and the marker is the corpus's own, read by the
   corpus's own reader, not a second spelling of `sim-fb-` that could drift.
4. **the tree matches the producer** — `--check` exits 0 on the bytes that are checked in.

Style follows ``services/sim/tests/test_seed_store.py``, which is the suite for the corpus this
panel is built out of.

Collection: ``scripts`` is NOT in ``[tool.pytest.ini_options] testpaths``, so a bare ``pytest``
does not collect this file. ``pytest scripts`` and ``pytest scripts/tests`` both do -- a path
named on the command line is collected regardless of ``testpaths``. This is stated rather than
worked around because ``pyproject.toml`` is not this ticket's to edit.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _producer() -> Any:
    """The producer module, loaded from its path.

    By path rather than by ``import scripts.build_seed_dashboard``: ``scripts/`` carries no
    ``__init__.py`` and is not on ``testpaths``, so importing it by name would depend on
    namespace-package resolution that a different pytest invocation might not give it. The path
    is a fact about the tree; the module name is a fact about the configuration.
    """
    path = REPO_ROOT / "scripts" / "build_seed_dashboard.py"
    spec = importlib.util.spec_from_file_location("_seed_dashboard_producer", path)
    assert spec is not None and spec.loader is not None, f"{path} is not importable"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def producer() -> Any:
    return _producer()


@pytest.fixture
def copied_panel(producer: Any, tmp_path: Path) -> Path:
    """The checked-in panel, copied somewhere a test may vandalise it."""
    destination = tmp_path / "merchant-dashboard"
    shutil.copytree(producer.DASHBOARD_ROOT, destination)
    return destination


# ------------------------------------------------------------------------------------
# 1. It loads, and only if its bytes match its own record
# ------------------------------------------------------------------------------------
def test_the_checked_in_panel_loads_and_every_digest_matches_its_bytes(producer: Any) -> None:
    """The whole point of the record: the bytes on disk are the bytes it accounts for."""
    panel = producer.load_panel()
    assert panel.collection["kind"] == producer.ARTIFACT_KIND
    assert panel.collection["simulated"] is True
    assert panel.payload["store_id"] == panel.collection["store_id"]


def test_the_recorded_digest_is_the_digest_of_the_payload_value_not_only_of_its_bytes(
    producer: Any,
) -> None:
    """No gap between "the digest of the value" and "the digest of the file".

    The payload is written in ``seed.store.canonical_bytes`` form, so the two are the same
    number. If they ever stop being the same number there is a representation an edit can hide
    in -- a re-indent that changes the bytes and not the value, or the reverse.
    """
    from seed.store import canonical_bytes

    panel = producer.load_panel()
    on_disk = (panel.root / producer.DASHBOARD_FILE).read_bytes()
    assert on_disk == canonical_bytes(panel.payload) + b"\n", (
        "dashboard-page.json is not in canonical form, so its file digest and its value digest "
        "are two different numbers and an edit could satisfy one without the other"
    )
    assert (
        panel.collection["determinism"]["canonical_digest"]
        == panel.collection["file_sha256"]["page"]
    )


def test_a_tampered_payload_is_refused_rather_than_loaded(
    producer: Any, copied_panel: Path
) -> None:
    """One changed number in the trust panel, and the whole artifact is refused."""
    path = copied_panel / producer.DASHBOARD_FILE
    body = json.loads(path.read_text(encoding="utf-8"))
    body["trust"]["snapshot"]["score"] = 0.99
    path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(producer.SeedPanelError) as refusal:
        producer.load_panel(copied_panel)
    assert "does not match the digest" in str(refusal.value)


def test_a_missing_digest_is_refused_exactly_as_a_wrong_one_is(
    producer: Any, copied_panel: Path
) -> None:
    """Deleting the digest must not read as "nothing to check".

    This is the bypass the load path exists to close. Every other check in this file can be
    defeated by an editor who ALSO edits ``collection.json`` -- except this one, which is the
    laziest version of that attack: change the payload, drop the one line that would catch it.
    """
    record = copied_panel / producer.COLLECTION_FILE
    collection = json.loads(record.read_text(encoding="utf-8"))
    del collection["file_sha256"]["page"]
    record.write_text(json.dumps(collection, indent=2, sort_keys=True) + "\n")

    with pytest.raises(producer.SeedPanelError) as refusal:
        producer.load_panel(copied_panel)
    assert "records no digest" in str(refusal.value)


def test_an_absent_payload_is_refused_rather_than_silently_empty(
    producer: Any, copied_panel: Path
) -> None:
    (copied_panel / producer.DASHBOARD_FILE).unlink()
    with pytest.raises(producer.SeedPanelError) as refusal:
        producer.load_panel(copied_panel)
    assert "is not there" in str(refusal.value)


def test_a_layout_this_reader_predates_is_refused_rather_than_misparsed(
    producer: Any, copied_panel: Path
) -> None:
    """A major-version gate, so a future layout is a sentence and not a wrong answer."""
    record = copied_panel / producer.COLLECTION_FILE
    collection = json.loads(record.read_text(encoding="utf-8"))
    collection["artifact_version"] = "2.0.0"
    record.write_text(json.dumps(collection, indent=2, sort_keys=True) + "\n")

    with pytest.raises(producer.SeedPanelError) as refusal:
        producer.load_panel(copied_panel)
    assert "artifact_version" in str(refusal.value)


# ------------------------------------------------------------------------------------
# 2. It is the shape the merchant dashboard actually serves
# ------------------------------------------------------------------------------------
def test_the_payload_is_a_complete_dashboard_page_with_every_panel_ok(producer: Any) -> None:
    """R9's whole page in one read. A refusal state would seed the demo with a misconfiguration."""
    page = producer.load_panel().payload
    assert set(page) == {
        "store_id",
        "generated_at",
        "onboarding",
        "envelope",
        "losses",
        "trust",
        "trust_events",
        "bids",
    }
    for name in ("onboarding", "envelope", "losses", "trust", "trust_events", "bids"):
        panel = page[name]
        assert panel["state"] == "ok", f"{name} is {panel['state']!r}: {panel.get('detail')}"
        assert isinstance(panel["detail"], str)


def test_the_trust_snapshot_carries_all_six_dimensions_and_no_mean(producer: Any) -> None:
    """D53: EXACTLY six, all required, and the wire carries alpha/beta only.

    The dimension list is imported rather than restated -- a second copy here would be a
    vocabulary free to drift out of the one the trust service and the contracts share. The "no
    mean" half is the measured one: ``apps/merchant/app/dashboard/api.ts`` records that an
    earlier draft expected a ``mean`` and the whole per-dimension table rendered empty.
    """
    from trust.scoring.dimensions import TRUST_DIMENSIONS

    snapshot = producer.load_panel().payload["trust"]["snapshot"]
    assert set(snapshot["dims"]) == set(TRUST_DIMENSIONS)
    for name, state in snapshot["dims"].items():
        assert set(state) == {"alpha", "beta", "decayed_at"}, f"{name} carries {sorted(state)}"
        assert state["alpha"] >= 0.0 and state["beta"] >= 0.0
        assert isinstance(state["decayed_at"], str) and state["decayed_at"]


def test_the_snapshot_validates_against_the_published_contract(producer: Any) -> None:
    """The page's trust row is a ``TrustSnapshot``, which is ``extra='forbid'``."""
    from contracts.protocol import TrustSnapshot

    TrustSnapshot.model_validate(producer.load_panel().payload["trust"]["snapshot"])


def test_the_loss_report_carries_the_four_closed_reasons_and_nothing_else(producer: Any) -> None:
    """``LossReasons`` is a CLOSED set of four, and there is no ``wins`` field anywhere.

    A new reason is a schema change, not a new dict key the merchant UI silently drops -- and
    a loss report is not a scoreboard: naming what a rival gained is what turns a seller
    feedback loop into a price-intelligence feed.
    """
    from contracts.protocol import LossReport

    losses = producer.load_panel().payload["losses"]
    LossReport.model_validate(
        {k: v for k, v in losses.items() if k in {"store_id", "window", "by_cluster"}}
    )
    assert losses["by_cluster"], "an empty loss list reads as 'you lost nothing'"
    for row in losses["by_cluster"]:
        assert set(row["reasons"]) == {"fit", "price", "commitments", "trust"}
    assert "wins" not in json.dumps(producer.load_panel().payload)


def test_every_solicitation_row_is_a_solicitation_row(producer: Any) -> None:
    """The keys ``apps/merchant/app/dashboard/api.ts`` declares, and the two closed vocabularies."""
    required = {
        "recorded_at",
        "origin",
        "store_id",
        "auction_id",
        "outcome",
        "activation",
        "may_bid",
        "claims",
        "contradiction",
    }
    for row in producer.load_panel().payload["bids"]["entries"]:
        assert required <= set(row), f"a bid row is missing {sorted(required - set(row))}"
        assert row["outcome"] in {"bid", "declined"}
        assert row["origin"] == "dashboard"
        if row["outcome"] == "declined":
            # A SEPARATE vocabulary from the loss reasons above, and it must stay separate:
            # `store_killed` is not a reason a shopper preferred somebody else.
            assert row["decline_reason"] in {
                "store_killed",
                "envelope_not_activated",
                "cluster_not_pursued",
            }
            assert "fit" != row["decline_reason"]


# ------------------------------------------------------------------------------------
# 3. Every manufactured row is marked
# ------------------------------------------------------------------------------------
def test_every_trust_event_carries_the_simulated_marker(producer: Any) -> None:
    """Read with the corpus's OWN reader, not with a second spelling of the prefix here.

    ``seed.provenance.is_seeded_event`` is the function the seed corpus's audit uses, and it
    reads ``order_ref`` at the top level first and in the payload second. Spelling ``sim-fb-``
    again in this file would be a second vocabulary free to drift from the writer's.
    """
    from seed.provenance import is_seeded_event

    events = producer.load_panel().payload["trust_events"]["events"]
    assert events, "a trust panel with no events marks nothing"
    unmarked = [event["event_id"] for event in events if not is_seeded_event(event)]
    assert unmarked == [], f"{len(unmarked)} events carry no manufactured marker: {unmarked[:3]}"


def test_the_trust_events_are_the_corpus_chain_and_not_a_retyping_of_it(producer: Any) -> None:
    """Byte-for-byte the rows in ``ledger.jsonl``, hashes and links included.

    This is what makes the marker a hash-chain fact rather than a claim: the events on the page
    are the sealed ones, so ``order_ref`` on them is covered by ``event_hash``.
    """
    from seed import store as seed_store

    panel = producer.load_panel()
    corpus = seed_store.load()
    store_id = panel.payload["store_id"]
    expected = [dict(event) for event in corpus.events if event.get("store_id") == store_id]
    assert panel.payload["trust_events"]["events"] == expected
    assert panel.payload["trust_events"]["count"] == len(expected)
    for event in expected:
        assert event["event_hash"] and event["prev_hash"], "an unsealed event proves nothing"


def test_every_rehearsal_row_carries_the_reserved_prefix(producer: Any) -> None:
    """The weaker marker, and the test says so: a prefix, in a value nothing hashes.

    It is still checkable -- a live dashboard mints ``auction_id`` from ``uuid.uuid4()``, whose
    string form cannot contain ``sim-fb-`` because ``s``, ``i`` and ``m`` are not hex digits --
    but an edit to one of these rows is caught only by the sha256 in ``collection.json``, which
    is exactly what ``marker.panels.bids.strength`` says.
    """
    from seed.provenance import SEED_MARKER_PREFIX

    panel = producer.load_panel()
    rows = panel.payload["bids"]["entries"]
    assert rows, "a bids panel with no rows demonstrates nothing"
    for row in rows:
        assert row["auction_id"].startswith(f"dashboard-rehearsal-{SEED_MARKER_PREFIX}")
    assert panel.collection["marker"]["panels"]["bids"]["strength"].startswith("reserved")


def test_the_marker_block_names_the_field_the_prefix_and_a_reader_that_exists(
    producer: Any,
) -> None:
    """A stranger with only this directory must be able to find the code that reads it."""
    import importlib

    from seed.provenance import SEED_MARKER_FIELD, SEED_MARKER_PREFIX

    marker = producer.load_panel().collection["marker"]
    assert marker["field"] == SEED_MARKER_FIELD
    assert marker["prefix"] == SEED_MARKER_PREFIX
    for dotted in (marker["reader"], marker["audit"]):
        module_name, _, attribute = dotted.rpartition(".")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{dotted} is not a reader"


def test_the_marker_is_honest_about_which_panels_carry_no_marker_at_all(producer: Any) -> None:
    """The whole reason the marker is a table and not a boolean.

    Three strengths sit on one page. A record that claimed the loss report was as well marked
    as the trust events would be overclaiming about the panel a merchant is most likely to
    quote back at somebody.
    """
    panels = producer.load_panel().collection["marker"]["panels"]
    assert panels["trust_events"]["strength"] == "hash-chain fact"
    assert panels["trust"]["strength"].startswith("hash-chain fact")
    assert panels["bids"]["strength"].startswith("reserved id prefix")
    for unmarked in ("losses", "envelope", "onboarding"):
        assert panels[unmarked]["strength"].startswith("none"), (
            f"{unmarked} is stated configuration and carries no marker; saying otherwise would "
            "be the overclaim this table exists to prevent"
        )


# ------------------------------------------------------------------------------------
# 4. The tree matches the producer
# ------------------------------------------------------------------------------------
def test_check_exits_zero_on_the_bytes_that_are_checked_in(producer: Any) -> None:
    """The gate itself, run against the tree.

    This rebuilds the whole page -- the real merchant routes, the real envelope algebra, a real
    store agent -- and compares. It is the one test here that would catch a producer whose
    output has drifted from the artifact somebody committed, which is the failure every other
    test in this file is blind to: they all read the same checked-in bytes.
    """
    assert producer.main(["--check"]) == 0


def test_the_producer_leaves_no_frozen_clock_behind_in_the_merchant_service(
    producer: Any,
) -> None:
    """A producer that froze a clock and did not put it back would poison the whole session."""
    import datetime as datetime_module

    import merchant_svc.dashboard.journal as journal
    import merchant_svc.dashboard.routes as routes

    with producer._frozen_service_clocks():
        assert routes.datetime is producer._FrozenClock
    assert routes.datetime is datetime_module.datetime
    assert journal._now() != producer.OBSERVED_AT
