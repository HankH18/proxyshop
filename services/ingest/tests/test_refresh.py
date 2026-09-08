"""The durable, cadence-driven refresh scheduler (T-024), graded on behaviour.

This is the file T-024's own ``verify`` names, and until now it did not exist — the gate ERRORED
at collection rather than grading anything, which reads in a summary exactly like a gate nobody
had got to yet. What it grades is the ticket's three acceptance criteria, each stated as a
property that a stub could not satisfy:

1. **Cadence config drives which adapters run**, on a frozen clock. Not "the config parses" —
   a tick where only ``offer.price`` has aged out must fetch ``/products.json`` and **not one
   policy page**, measured on the served storefront's own request log.
2. **``POST /refresh/{store}`` triggers one store's pipeline.** Still true, still hand-triggerable,
   and now sharing its whole body with the scheduler rather than being replaced by it.
3. **No re-extraction without a hash change across a full cycle** — including across a *restart*,
   which is the case a process-local ledger silently fails and the one "durable" is for.

Three properties of the tests themselves, because this repo has shipped gates that could not fail:

**Every durability claim has a control.** "The restarted scheduler did not re-crawl" is only
evidence if a scheduler with an *empty* state file, on the same clock and the same storefront,
does re-crawl. Both are asserted side by side; without the control the test would pass against a
scheduler that had simply stopped working.

**Nothing sleeps.** Every schedule decision is driven through ``proxyshop_support.clock.ManualClock``
— the repo's existing injectable clock seam, not a second one invented here — so a weekly cadence
is exercised in microseconds and there is no tolerance window for a flaky pass to hide in.

**Nothing leaves this process.** The storefront is served in-process on loopback (D40), the graph
is an injected session double, and no test needs Neo4j — a skipped test is not a pass (T-245), so
nothing here is allowed to depend on the compose stack being up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ingest.adapters import FetchPolicy
from ingest.scheduler.cadence import (
    CADENCE_ENV,
    DEFAULT_CADENCE,
    POLICIES,
    PRODUCTS,
    CadenceConfig,
    FieldCadence,
)
from ingest.scheduler.catalog import CatalogRefreshRunner, StoreRegistry, StoreTarget
from ingest.scheduler.cycle import RefreshScheduler, SystemClock
from ingest.scheduler.state import (
    STATE_ENV,
    JsonFileScheduleStore,
    MemoryScheduleStore,
    ScheduleState,
    build_schedule_store,
)

from proxyshop_support.clock import EPOCH, ManualClock

#: Loopback is not publicly routable, so the default SSRF posture refuses the in-process
#: storefront. Same constant the T-236 suite next door uses, for the same reason.
LOOPBACK = FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",))

HOUR = 3_600

#: A cadence table whose whole cycle is four hours instead of a week, so "across a full cycle"
#: is four ticks rather than 169. The *shape* is the default's — two fast catalog fields, one
#: slow catalog field, one policy field on the longest interval — because that shape is what
#: the assertions are about; the numbers are configuration, which is the ticket's point.
FAST_CADENCE = CadenceConfig(
    (
        FieldCadence(field="offer.price", section=PRODUCTS, max_age_seconds=HOUR),
        FieldCadence(field="offer.availability", section=PRODUCTS, max_age_seconds=HOUR),
        FieldCadence(field="product.title", section=PRODUCTS, max_age_seconds=2 * HOUR),
        FieldCadence(field="policy.claims", section=POLICIES, max_age_seconds=4 * HOUR),
    )
)


class _CapturingSession:
    """A stand-in Neo4j session that records the Cypher it is asked to run."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, /, **params: Any) -> Any:
        self.queries.append(str(query))
        return _CapturingResult(params)

    def __enter__(self) -> _CapturingSession:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


class _CapturingResult:
    def __init__(self, params: dict[str, Any]) -> None:
        self._params = params

    def single(self) -> _Row:
        return _Row({k: v for k, v in self._params.items() if k != "props"})

    def consume(self) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(())


class _Row(dict):
    """A result row whose absent columns read as ``1``.

    ``ingest.graph.upsert`` guards every write with an existence count and raises when it is
    zero, so a stand-in answering ``0`` would make these tests assert that the write path
    refuses everything. The graph's own refusal behaviour is covered against a real Neo4j in
    ``test_graph.py``.
    """

    def __missing__(self, key: str) -> int:
        return 1


class _SessionFactory:
    """A session factory that counts how many times a session was actually opened."""

    def __init__(self) -> None:
        self.opened = 0
        self.sessions: list[_CapturingSession] = []

    def __call__(self) -> _CapturingSession:
        self.opened += 1
        session = _CapturingSession()
        self.sessions.append(session)
        return session


class _Recorder:
    """A ``perform`` double that records what the scheduler asked it to refresh.

    The scheduler's whole decision is *which sections for which store*, so recording exactly
    that — and nothing else — is what makes the cadence assertions read as the property rather
    than as a crawl's side effects.
    """

    def __init__(self, explode: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.explode = explode

    def __call__(self, store_id: str, sections: Any, *, force: bool = False) -> Any:
        self.calls.append((store_id, tuple(sections)))
        if self.explode is not None:
            raise self.explode
        return None

    @property
    def sections_for(self) -> dict[str, list[tuple[str, ...]]]:
        seen: dict[str, list[tuple[str, ...]]] = {}
        for store_id, sections in self.calls:
            seen.setdefault(store_id, []).append(sections)
        return seen


def _target(base_url: str, store_id: str = "store-1", **kwargs: Any) -> StoreTarget:
    return StoreTarget(store_id=store_id, base_url=base_url, **kwargs)


def _registry(*store_ids: str) -> StoreRegistry:
    return StoreRegistry(
        [StoreTarget(store_id=sid, base_url=f"https://{sid}.example.com/") for sid in store_ids]
    )


def _scheduler(
    registry: StoreRegistry,
    perform: Any,
    *,
    clock: ManualClock,
    cadence: CadenceConfig | None = None,
    path: Path | None = None,
    runner: Any = None,
) -> RefreshScheduler:
    return RefreshScheduler(
        registry=registry,
        perform=perform,
        cadence=cadence if cadence is not None else FAST_CADENCE,
        store=JsonFileScheduleStore(path) if path is not None else MemoryScheduleStore(),
        runner=runner,
        clock=clock,
    )


def _products_perform(runner: CatalogRefreshRunner) -> Any:
    """A ``perform`` that runs only the catalog half, through the real runner."""

    def perform(store_id: str, sections: Any, *, force: bool = False) -> Any:
        assert PRODUCTS in tuple(sections), f"expected a products run, got {tuple(sections)}"
        return runner.refresh(store_id, force=force)

    return perform


# ---------------------------------------------------------------------------------------
# 1. cadence is CONFIGURATION, and it is field-appropriate
# ---------------------------------------------------------------------------------------


def test_the_default_cadence_gives_different_fields_different_intervals() -> None:
    """The whole premise: one global interval is the wrong shape and the table says so.

    A table where every field shared one interval would satisfy "a cadence config exists" and
    deliver nothing the ticket asks for, so this asserts the spread rather than the presence.
    """
    config = CadenceConfig()
    intervals = {entry.field: entry.max_age_seconds for entry in config}

    assert len(set(intervals.values())) > 1, (
        f"every field shares one interval {intervals}; that is a global refresh clock with a "
        f"config file in front of it, not a field-appropriate cadence"
    )
    assert intervals["offer.price"] < intervals["product.title"], (
        f"a price is not scheduled more often than a title: {intervals}"
    )
    assert intervals["product.title"] < intervals["policy.claims"], (
        f"a policy page is not the slowest-moving surface: {intervals}"
    )
    assert set(config.sections) == {PRODUCTS, POLICIES}, config.sections


def test_the_cadence_is_read_from_the_environment_rather_than_compiled_in() -> None:
    """D41: how often to crawl a particular merchant is a deployment fact, not a source fact."""
    configured = CadenceConfig.from_env(
        {
            CADENCE_ENV: json.dumps(
                [
                    {"field": "offer.price", "section": PRODUCTS, "max_age_seconds": 900},
                    {"field": "policy.claims", "section": POLICIES, "max_age_seconds": 86_400},
                ]
            )
        }
    )

    assert configured.fields == ("offer.price", "policy.claims"), configured.fields
    assert configured.entries[0].max_age_seconds == 900
    assert configured != CadenceConfig(), (
        "the configured table is identical to the default, so nothing was actually read"
    )


def test_the_cadence_can_be_a_file_an_operator_mounts(tmp_path: Path) -> None:
    """Fifty fields do not belong in an environment variable; a mounted file is the other form."""
    path = tmp_path / "cadence.json"
    path.write_text(
        json.dumps([{"field": "offer.price", "section": PRODUCTS, "max_age_seconds": 60}]),
        encoding="utf-8",
    )

    configured = CadenceConfig.from_env({CADENCE_ENV: str(path)})

    assert configured.fields == ("offer.price",)
    assert configured.entries[0].max_age_seconds == 60


def test_an_unusable_cadence_keeps_the_defaults_instead_of_scheduling_nothing() -> None:
    """The failure mode this choice exists to avoid is invisible from outside.

    An empty table means *no field is ever due*, so the scheduler would run forever, refresh
    nothing, and look exactly like a healthy idle service. Keeping the defaults and warning is
    loud; silently freezing the graph is not.
    """
    warnings: list[str] = []
    config = CadenceConfig.from_env({CADENCE_ENV: "{not json"}, warnings=warnings)

    assert config.entries == DEFAULT_CADENCE, "unparseable config did not fall back to defaults"
    assert warnings and "not valid JSON" in warnings[0], warnings
    assert any("default cadence" in w for w in warnings), warnings

    # A brace is unambiguously an attempt at JSON, so it must not be reported as a missing FILE —
    # that sends an operator looking for a path they never wrote.
    assert "file" not in warnings[0], warnings

    missing: list[str] = []
    assert (
        CadenceConfig.from_env({CADENCE_ENV: "/no/such/cadence.json"}, warnings=missing).entries
        == DEFAULT_CADENCE
    )
    assert any("could not be read" in w for w in missing), missing


def test_a_partly_bad_cadence_keeps_the_entries_that_parsed_and_names_the_rest() -> None:
    """Operator intent is honoured as far as it goes; what was dropped is named."""
    warnings: list[str] = []
    config = CadenceConfig.from_payload(
        [
            {"field": "offer.price", "section": PRODUCTS, "max_age_seconds": 300},
            {"field": "offer.price", "section": "carrier_pigeon", "max_age_seconds": 300},
            {"field": "product.title", "section": PRODUCTS, "max_age_seconds": 0},
            {"field": "policy.claims", "secton": POLICIES, "max_age_seconds": 60},
        ],
        warnings=warnings,
    )

    assert config.fields == ("offer.price",), config.fields
    assert any("[1]" in w and "carrier_pigeon" in w for w in warnings), warnings
    assert any("[2]" in w and "at least 1" in w for w in warnings), warnings
    assert any("[3]" in w and "secton" in w for w in warnings), warnings


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"field": "", "section": PRODUCTS, "max_age_seconds": 60}, "non-empty"),
        ({"field": "x", "section": "nowhere", "max_age_seconds": 60}, "not one of"),
        ({"field": "x", "section": PRODUCTS, "max_age_seconds": 0}, "at least 1"),
        ({"field": "x", "section": PRODUCTS, "max_age_seconds": -1}, "at least 1"),
        ({"field": "x", "section": PRODUCTS, "max_age_seconds": True}, "not a boolean"),
        ({"field": "x", "section": PRODUCTS, "max_age_seconds": "soon"}, "whole number"),
    ],
)
def test_a_cadence_entry_that_could_not_schedule_anything_is_refused(
    kwargs: dict[str, Any], match: str
) -> None:
    """Each of these is a configuration mistake that would otherwise become crawl behaviour.

    ``max_age_seconds: true`` is the nastiest: ``int(True) == 1``, so without the boolean check
    a JSON typo configures a field that is overdue every second — one crawl per second, at
    somebody else's storefront.
    """
    with pytest.raises(ValueError, match=match):
        FieldCadence(**kwargs)


def test_a_cadence_table_cannot_be_empty_or_name_a_field_twice() -> None:
    """Both are configurations with no defined behaviour, so neither is accepted."""
    with pytest.raises(ValueError, match="at least one field"):
        CadenceConfig(())
    with pytest.raises(ValueError, match="twice"):
        CadenceConfig(
            (
                FieldCadence(field="offer.price", section=PRODUCTS, max_age_seconds=60),
                FieldCadence(field="offer.price", section=PRODUCTS, max_age_seconds=600),
            )
        )


# ---------------------------------------------------------------------------------------
# 2. the cadence drives WHICH ADAPTERS RUN, on a frozen clock  (acceptance 1)
# ---------------------------------------------------------------------------------------


def test_a_store_nothing_has_aged_out_of_is_not_touched_at_all(manual_clock: ManualClock) -> None:
    """ "Current" has to mean *no adapter runs*, not "a cheaper run happens"."""
    recorder = _Recorder()
    scheduler = _scheduler(_registry("store-1"), recorder, clock=manual_clock)

    first = scheduler.tick()
    manual_clock.advance(60)
    second = scheduler.tick()

    assert [run.store_id for run in first.ran] == ["store-1"]
    assert second.ran == (), f"a store refreshed 60s ago was refreshed again: {second.ran}"
    assert [skip.store_id for skip in second.skipped] == ["store-1"]
    assert len(recorder.calls) == 1, recorder.calls


def test_only_the_adapters_the_overdue_fields_need_are_run(manual_clock: ManualClock) -> None:
    """The ticket's first acceptance criterion, stated as the thing it buys.

    ``offer.price`` ages out hourly and ``policy.claims`` every four hours. One hour after a
    full run, the *only* thing that has gone stale is a catalog field — so the tick must run the
    catalog adapter and must not fetch a policy page. A scheduler that ran everything whenever
    anything was due would pass a "cadence config exists" test and re-crawl every policy page
    hourly, which is the cost the whole ticket is about.
    """
    recorder = _Recorder()
    scheduler = _scheduler(_registry("store-1"), recorder, clock=manual_clock)

    scheduler.tick()  # first ever run: nothing is remembered, so everything is due
    manual_clock.advance(HOUR)
    scheduler.tick()  # only the hourly catalog fields have aged out
    manual_clock.advance(3 * HOUR)
    scheduler.tick()  # now the four-hourly policy field has too

    assert recorder.sections_for["store-1"] == [
        (PRODUCTS, POLICIES),
        (PRODUCTS,),
        (PRODUCTS, POLICIES),
    ], recorder.sections_for


def test_a_products_run_also_makes_the_slow_catalog_fields_fresh(
    manual_clock: ManualClock,
) -> None:
    """One crawl re-reads every catalog field, so the bookkeeping must credit all of them.

    ``product.title`` is on a two-hour cadence but it is read by the same ``/products.json``
    fetch that ``offer.price`` triggers hourly. Stamping only the fields that happened to be
    *due* would leave the title claiming a staleness the crawl had already fixed — and it would
    re-trigger a redundant run at the two-hour mark.
    """
    recorder = _Recorder()
    scheduler = _scheduler(_registry("store-1"), recorder, clock=manual_clock)

    scheduler.tick()
    manual_clock.advance(HOUR)
    scheduler.tick()

    plan = scheduler.plan_for("store-1")
    title = next(status for status in plan.fields if status.field == "product.title")
    price = next(status for status in plan.fields if status.field == "offer.price")

    assert title.last_refreshed == manual_clock.now(), (
        f"the hourly crawl did not credit the two-hourly field it also re-read: "
        f"{title.last_refreshed} vs {manual_clock.now()}"
    )
    assert title.last_refreshed == price.last_refreshed, (
        "two fields read by one crawl carry different freshness"
    )
    assert title.due_at == EPOCH.replace(hour=3), title.due_at


def test_each_store_is_scheduled_on_its_own_clock(manual_clock: ManualClock) -> None:
    """Two stores registered at different times must not be yoked together."""
    registry = _registry("store-1")
    recorder = _Recorder()
    scheduler = _scheduler(registry, recorder, clock=manual_clock)

    scheduler.tick()
    manual_clock.advance(HOUR // 2)
    registry.register(StoreTarget(store_id="store-2", base_url="https://two.example.com/"))
    second = scheduler.tick()

    assert [run.store_id for run in second.ran] == ["store-2"], (
        f"the newly registered store did not run alone: {[r.store_id for r in second.ran]}"
    )
    assert [skip.store_id for skip in second.skipped] == ["store-1"]


def test_a_forced_tick_runs_every_store_regardless_of_the_clock(
    manual_clock: ManualClock,
) -> None:
    """The operator override, and the arming control for every "nothing was due" assertion."""
    recorder = _Recorder()
    scheduler = _scheduler(_registry("store-1"), recorder, clock=manual_clock)

    scheduler.tick()
    manual_clock.advance(60)
    assert scheduler.tick().ran == (), "the control is broken: this store was already current"

    forced = scheduler.tick(force=True)

    assert [run.store_id for run in forced.ran] == ["store-1"]
    assert forced.ran[0].sections == (PRODUCTS, POLICIES), forced.ran[0].sections


def test_a_refresh_that_raises_leaves_the_store_due_instead_of_marking_it_fresh(
    manual_clock: ManualClock,
) -> None:
    """A store that could not be read is still stale, and the next tick must try again.

    Stamping the fields fresh because a refresh was *attempted* makes an unreachable merchant
    look permanently current — a graph that is confidently wrong rather than visibly stale.
    """
    exploding = _Recorder(explode=ConnectionError("the storefront is not answering"))
    scheduler = _scheduler(_registry("store-1"), exploding, clock=manual_clock)

    report = scheduler.tick()

    assert report.ran == (), report.ran
    assert [failure.store_id for failure in report.failed] == ["store-1"]
    assert "ConnectionError" in report.failed[0].error
    assert scheduler.plan_for("store-1").due_sections == (PRODUCTS, POLICIES), (
        "a failed refresh advanced the store's refresh clock"
    )

    exploding.explode = None
    recovered = scheduler.tick()
    assert [run.store_id for run in recovered.ran] == ["store-1"], "the retry never happened"


# ---------------------------------------------------------------------------------------
# 3. DURABLE — the state survives the process
# ---------------------------------------------------------------------------------------


def test_the_refresh_clock_survives_a_restart_and_a_fresh_one_does_not(tmp_path: Path) -> None:
    """The load-bearing word in the ticket, with its own control beside it.

    Scheduler A refreshes the store and dies. Scheduler B is a *different object* reading the
    same file, half an hour later, and must leave the store alone. Scheduler C is identical to B
    except that its state file is empty — and it must refresh, or B's silence would be evidence
    of nothing at all.
    """
    path = tmp_path / "schedule.json"
    registry = _registry("store-1")

    first = _Recorder()
    _scheduler(registry, first, clock=ManualClock(EPOCH), path=path).tick()
    assert [call[0] for call in first.calls] == ["store-1"], first.calls

    later = ManualClock(EPOCH)
    later.advance(HOUR // 2)

    restarted = _Recorder()
    report = _scheduler(registry, restarted, clock=later, path=path).tick()

    assert restarted.calls == [], (
        "a restarted process re-crawled a store refreshed 30 minutes ago; the schedule did not "
        "survive the process"
    )
    assert [skip.store_id for skip in report.skipped] == ["store-1"]

    # The control. Same clock, same registry, same cadence — only the state file differs.
    control = _Recorder()
    _scheduler(registry, control, clock=later, path=tmp_path / "empty.json").tick()
    assert [call[0] for call in control.calls] == ["store-1"], (
        "the control did not refresh either, so the assertion above proves nothing about "
        "persistence — the scheduler may simply have stopped working"
    )


def test_the_state_file_really_holds_the_per_field_timestamps(tmp_path: Path) -> None:
    """Read the file, not the object: the readback is the whole durability claim."""
    path = tmp_path / "nested" / "schedule.json"
    _scheduler(_registry("store-1"), _Recorder(), clock=ManualClock(EPOCH), path=path).tick()

    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["version"] == 1, document
    stamped = document["stores"]["store-1"]["last_refreshed"]
    assert set(stamped) == set(FAST_CADENCE.fields), stamped
    assert all(value.startswith("2026-01-01T00:00:00") for value in stamped.values()), stamped
    assert not list(path.parent.glob("*.tmp")), (
        f"the atomic write left a temp file behind: {list(path.parent.iterdir())}"
    )


def test_a_corrupt_state_file_is_moved_aside_rather_than_read_or_silently_overwritten(
    tmp_path: Path,
) -> None:
    """Starting over is survivable; starting over with no evidence of why is not."""
    path = tmp_path / "schedule.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = JsonFileScheduleStore(path)

    state = store.load()

    assert state.stores == {}, state.stores
    assert (tmp_path / "schedule.json.corrupt").read_text(encoding="utf-8") == "{ this is not json"


def test_a_state_document_from_a_future_version_is_not_misread(tmp_path: Path) -> None:
    """A shape this build was not written for is refused, not guessed at."""
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps({"version": 99, "stores": {"store-1": {}}}), encoding="utf-8")

    assert JsonFileScheduleStore(path).load().stores == {}


def test_an_unconfigured_state_path_is_reported_as_non_durable_rather_than_faked() -> None:
    """The one thing worse than no persistence is persistence an operator believes in."""
    warnings: list[str] = []
    store = build_schedule_store({}, warnings=warnings)

    assert isinstance(store, MemoryScheduleStore)
    assert store.durable is False
    assert any(STATE_ENV in w and "resets on restart" in w for w in warnings), warnings


def test_a_configured_state_path_is_durable(tmp_path: Path) -> None:
    warnings: list[str] = []
    store = build_schedule_store({STATE_ENV: str(tmp_path / "s.json")}, warnings=warnings)

    assert isinstance(store, JsonFileScheduleStore)
    assert store.durable is True
    assert warnings == []


def test_a_saved_state_round_trips_through_the_file_unchanged(tmp_path: Path) -> None:
    """The serialiser and the parser have to agree, or durability is a slow data-loss bug."""
    store = JsonFileScheduleStore(tmp_path / "schedule.json")
    state = ScheduleState()
    schedule = state.for_store("store-1")
    schedule.last_refreshed["offer.price"] = EPOCH
    schedule.hashes["product/1"] = "sha256:abc"
    store.save(state)

    reloaded = JsonFileScheduleStore(tmp_path / "schedule.json").load()

    assert reloaded.to_payload() == state.to_payload()
    assert reloaded.stores["store-1"].last_refreshed["offer.price"] == EPOCH
    assert reloaded.stores["store-1"].hashes == {"product/1": "sha256:abc"}


# ---------------------------------------------------------------------------------------
# 4. no re-extraction without a hash change — including across a restart  (acceptance 3)
# ---------------------------------------------------------------------------------------


def test_a_restarted_scheduler_does_not_re_extract_a_catalog_nobody_touched(
    storefront: Any, tmp_path: Path
) -> None:
    """The differential guarantee, across the boundary that used to break it.

    ``CatalogRefreshRunner.hashes`` lives in the runner and dies with the process, so before
    T-024 the first refresh after every redeploy re-extracted a catalog nobody had touched — and
    "no re-extraction without a hash change **across a full cycle**" was only ever true within
    one process's lifetime. The ledger is now persisted beside the refresh clock and pushed back
    into a fresh runner on startup.

    The control is a third process with an empty state file on the same clock and the same
    storefront: it must open a graph session, or the silence above is not evidence.
    """
    base_url, _stub = storefront
    path = tmp_path / "schedule.json"
    registry = StoreRegistry([_target(base_url)])

    def process(state_path: Path, clock: ManualClock) -> tuple[_SessionFactory, RefreshScheduler]:
        sessions = _SessionFactory()
        runner = CatalogRefreshRunner(registry=registry, policy=LOOPBACK, session_factory=sessions)
        scheduler = _scheduler(
            registry,
            _products_perform(runner),
            clock=clock,
            path=state_path,
            runner=runner,
        )
        return sessions, scheduler

    first_sessions, first = process(path, ManualClock(EPOCH))
    first.tick()
    assert first_sessions.opened == 1, "the first process did no graph work; nothing to compare"

    # A different process: new runner, new empty in-memory ledger, same state file, and far
    # enough past the hourly price cadence that the store is genuinely due for a crawl.
    later = ManualClock(EPOCH)
    later.advance(2 * HOUR)
    restarted_sessions, restarted = process(path, later)
    report = restarted.tick()

    assert [run.store_id for run in report.ran] == ["store-1"], (
        "the restarted process skipped the store entirely, so this measures the refresh clock "
        "rather than the hash ledger"
    )
    assert restarted_sessions.opened == 0, (
        "the restarted process re-extracted an unchanged catalog: the differential ledger did "
        "not survive the restart"
    )

    control_sessions, control = process(tmp_path / "empty.json", later)
    control.tick()
    assert control_sessions.opened == 1, (
        "the control did not re-extract either, so the zero above proves nothing — the crawl "
        "may simply have stopped working"
    )


def test_a_full_cadence_cycle_of_an_unchanged_store_does_the_graph_work_once(
    storefront: Any,
) -> None:
    """Acceptance 3, taken literally: a whole cycle, hour by hour, on the frozen clock.

    Five ticks spanning the longest cadence in the table. The first does the work; the other
    four re-crawl a store nobody has touched and must write nothing at all — not fewer writes,
    *none*, including opening no graph session. Then one variant price changes and the very next
    catalog tick writes again, which is what stops this from being a test that a broken crawl
    would also pass.
    """
    base_url, stub = storefront
    clock = ManualClock(EPOCH)
    sessions = _SessionFactory()
    registry = StoreRegistry([_target(base_url)])
    runner = CatalogRefreshRunner(registry=registry, policy=LOOPBACK, session_factory=sessions)
    scheduler = _scheduler(registry, _products_perform(runner), clock=clock, runner=runner)

    scheduler.tick()
    assert sessions.opened == 1, "the first tick did no graph work; the cycle measures nothing"

    for _hour in range(4):
        clock.advance(HOUR)
        report = scheduler.tick()
        assert [run.store_id for run in report.ran] == ["store-1"], report.as_dict()

    assert sessions.opened == 1, (
        f"a full cadence cycle over an unchanged store opened {sessions.opened} graph sessions; "
        f"unchanged content must re-extract nothing"
    )
    crawls = stub.paths_fetched().count("/products.json")
    assert crawls == 5, f"the store was crawled {crawls} times across five ticks"

    stub.products[0]["variants"][0]["price"] = "139.95"
    clock.advance(HOUR)
    scheduler.tick()

    assert sessions.opened == 2, (
        "a changed price did not reach the graph; the ledger is now suppressing real changes"
    )


# ---------------------------------------------------------------------------------------
# 5. the served surface — the route an operator or a cron entry actually calls
# ---------------------------------------------------------------------------------------


@pytest.fixture
def scheduled_client(storefront: Any, tmp_path: Path) -> Any:
    """The real app with a frozen-clock scheduler over a served storefront.

    Everything swapped here is module state that the route module holds on purpose, and every
    piece of it is put back: ``test_scheduler_refresh.py``'s leak guard runs after this file and
    fails on anything left behind.
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from ingest.main import create_app  # noqa: PLC0415
    from ingest.scheduler import routes  # noqa: PLC0415

    base_url, stub = storefront
    sessions = _SessionFactory()
    clock = ManualClock(EPOCH)

    saved = (
        routes.runner.registry,
        routes.runner.policy,
        routes.runner.session_factory,
        routes.scheduler,
    )
    routes.registry.register(_target(base_url))
    routes.runner.registry = routes.registry
    routes.runner.policy = LOOPBACK
    routes.runner.session_factory = sessions
    routes.runner.forget("store-1")
    routes.scheduler = RefreshScheduler(
        registry=routes.registry,
        # The PRODUCTION function, not a double: the scheduler being another caller of the
        # hand-triggered path is the property, so a test that routed around it would measure a
        # scheduler this service does not ship.
        perform=routes.perform_refresh,
        cadence=FAST_CADENCE,
        store=JsonFileScheduleStore(tmp_path / "schedule.json"),
        runner=routes.runner,
        clock=clock,
    )
    try:
        yield TestClient(create_app()), stub, sessions, clock
    finally:
        (
            routes.runner.registry,
            routes.runner.policy,
            routes.runner.session_factory,
            routes.scheduler,
        ) = saved
        routes.registry.unregister("store-1")
        routes.runner.forget("store-1")
        routes._ingestors.pop("store-1", None)


def test_the_schedule_routes_are_served_by_the_frozen_entrypoints_own_discovery() -> None:
    """No wiring anywhere but the ``*/routes.py`` glob ``main.py`` already performs.

    A scheduler nothing serves is the island this project has produced repeatedly: built,
    imported, tested and reachable by nobody.
    """
    from ingest import main  # noqa: PLC0415

    app = main.create_app()
    served = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method.lower() in {"get", "post"}
    }

    assert "ingest.scheduler.routes" in app.state.mounted_routers
    assert ("GET", "/schedule") in served, sorted(served)
    assert ("POST", "/schedule/tick") in served, sorted(served)
    assert ("POST", "/refresh/{store_id}") in served, (
        "the hand-triggered refresh stopped being served; the scheduler replaced the operator "
        "path instead of joining it"
    )


def test_the_tick_route_refreshes_only_what_the_cadence_says_is_stale(
    scheduled_client: Any,
) -> None:
    """Acceptance 1 through the door, measured on the storefront's own request log.

    A recorder can be fooled by a scheduler that computes the right sections and then crawls
    everything anyway. The store cannot: after the first full tick, an hour later, it must be
    asked for ``/products.json`` and for **no policy page at all**.
    """
    client, stub, _sessions, clock = scheduled_client

    first = client.post("/schedule/tick", json={})
    assert first.status_code == 200, first.text
    assert [run["sections"] for run in first.json()["ran"]] == [[PRODUCTS, POLICIES]], first.text
    assert any(p.startswith(("/policies/", "/pages/")) for p in stub.paths_fetched()), (
        f"the first tick never fetched a policy page: {sorted(set(stub.paths_fetched()))}"
    )

    before = list(stub.paths_fetched())
    clock.advance(HOUR)
    second = client.post("/schedule/tick", json={})

    assert second.status_code == 200, second.text
    assert [run["sections"] for run in second.json()["ran"]] == [[PRODUCTS]], second.text
    fetched_since = stub.paths_fetched()[len(before) :]
    assert "/products.json" in fetched_since, fetched_since
    assert not [p for p in fetched_since if p.startswith(("/policies/", "/pages/"))], (
        f"a tick where only catalog fields had aged out still crawled policy pages: {fetched_since}"
    )


def test_the_tick_route_leaves_a_current_store_alone(scheduled_client: Any) -> None:
    """The 200 says what happened, so "nothing was due" is distinguishable from "it ran"."""
    client, stub, _sessions, clock = scheduled_client

    client.post("/schedule/tick", json={})
    before = len(stub.requests)
    clock.advance(60)
    response = client.post("/schedule/tick", json={})

    body = response.json()
    assert body["ran"] == [], body
    assert [skip["store_id"] for skip in body["skipped"]] == ["store-1"], body
    assert body["skipped"][0]["next_due_at"], body
    assert len(stub.requests) == before, "a skipped store was still asked for something"


def test_the_schedule_route_reports_when_each_field_next_goes_stale(
    scheduled_client: Any,
) -> None:
    """The operator readback: is the graph current, and if not, when will it be?"""
    client, _stub, _sessions, clock = scheduled_client

    cold = client.get("/schedule").json()
    assert cold["stores"][0]["due"] is True, cold
    assert all(field["last_refreshed"] is None for field in cold["stores"][0]["fields"]), cold

    client.post("/schedule/tick", json={})
    clock.advance(HOUR // 2)
    warm = client.get("/schedule").json()

    store = warm["stores"][0]
    assert store["store_id"] == "store-1"
    assert store["due"] is False, store
    assert store["due_sections"] == [], store
    due_at = {field["field"]: field["due_at"] for field in store["fields"]}
    assert due_at["offer.price"] == "2026-01-01T01:00:00+00:00", due_at
    assert due_at["policy.claims"] == "2026-01-01T04:00:00+00:00", due_at
    assert warm["durable"] is True, warm
    assert [entry["field"] for entry in warm["cadence"]] == list(FAST_CADENCE.fields), warm


def test_the_hand_triggered_refresh_still_works_beside_the_scheduler(
    scheduled_client: Any,
) -> None:
    """Acceptance 2, and the promise that the scheduler did not replace the operator path.

    Someone who has just been told a merchant changed their returns policy must not have to wait
    for a cadence window. The manual route also drops the ledger on ``force``, which the tick
    deliberately does not.
    """
    client, stub, sessions, _clock = scheduled_client

    response = client.post("/refresh/store-1", json={"sections": ["products"]})

    assert response.status_code == 202, response.text
    assert set(response.json()) == {"store_id", "job_id", "provenance"}, response.json()
    assert sessions.opened == 1, "the hand-triggered refresh did not reach the graph write path"
    assert "/products.json" in stub.paths_fetched()

    # No clock movement at all, so nothing is due — yet a forced hand refresh still crawls.
    forced = client.post("/refresh/store-1", json={"sections": ["products"], "force": True})
    assert forced.status_code == 202, forced.text
    assert sessions.opened == 2, "force did not re-read the catalog through the manual door"


def test_a_tick_records_its_work_where_the_next_process_will_read_it(
    scheduled_client: Any, tmp_path: Path
) -> None:
    """The served tick writes the durable file, not just an in-process dict."""
    client, _stub, _sessions, _clock = scheduled_client

    client.post("/schedule/tick", json={})

    document = json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))
    assert set(document["stores"]["store-1"]["last_refreshed"]) == set(FAST_CADENCE.fields)
    assert document["stores"]["store-1"]["hashes"], (
        "the tick persisted no differential ledger, so a restart would re-extract everything"
    )


def test_the_tick_body_refuses_a_field_this_service_does_not_implement(
    scheduled_client: Any,
) -> None:
    """A 200 for work that did not happen is worse than a refusal."""
    client, _stub, _sessions, _clock = scheduled_client
    response = client.post("/schedule/tick", json={"store_id": "store-1"})
    assert response.status_code == 422, response.text


def test_the_default_scheduler_uses_the_wall_clock() -> None:
    """The frozen clock is a test seam, not the shipped behaviour.

    Every other test here drives a ``ManualClock``, which would be a comfortable way to ship a
    scheduler whose production clock never moves.
    """
    scheduler = RefreshScheduler(registry=StoreRegistry(), perform=_Recorder())
    assert isinstance(scheduler.clock, SystemClock)
    assert scheduler.clock.now().tzinfo is not None


def test_the_verifiers_stock_freshness_floor_matches_this_services_own_cadence() -> None:
    """D59's drift gate. The claim verifier declines to grade a stock claim against an
    availability reading older than ``claim_verification.verifier.STOCK_EVIDENCE_WINDOW_DAYS``,
    and the whole defence of that number is that it is not a number the verifier invented: it
    is the interval THIS service publishes for re-reading ``offer.availability``. The platform
    holds its own grading to the freshness standard it set for its own crawler.

    Two definitions in two packages is exactly the shape that drifts, and a drift here is
    silent in both directions — loosen the cadence and the verifier starts grading readings the
    crawler has already given up on; tighten it and every stock claim quietly becomes
    ``unsupported`` again, which is the hole D59 closed.

    **What this gate does NOT cover, stated so nobody reads it as more than it is:** it pins the
    DEFAULT table, and a deployment may retune ``offer.availability`` through
    ``CadenceConfig.from_env`` (``PROXYSHOP_INGEST_CADENCE``). An operator who does that moves
    the crawler and not the verifier, and this stays green. Binding the two at runtime means the
    verifier reading a deployment's cadence config, which is a dependency from a leaf package
    onto a service and a separate decision; D59 records it as a residual.
    """
    from claim_verification.verifier import STOCK_EVIDENCE_WINDOW_DAYS  # noqa: PLC0415

    published = next(entry for entry in DEFAULT_CADENCE if entry.field == "offer.availability")
    assert STOCK_EVIDENCE_WINDOW_DAYS == pytest.approx(published.max_age_seconds / 86_400.0), (
        f"the verifier grades a stock claim on a reading up to "
        f"{STOCK_EVIDENCE_WINDOW_DAYS * 86_400.0:.0f}s old while this service refreshes "
        f"`offer.availability` every {published.max_age_seconds}s; one of the two moved"
    )
