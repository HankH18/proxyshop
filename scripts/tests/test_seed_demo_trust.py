"""The demo's trust posture: is it reachable, is it marked, and does it leave room to move?

Four groups, and they are the four claims `scripts/seed_demo_trust.py` makes:

1. **the deployment states no `trust_snapshot`** — the regression that this whole change exists
   to prevent. `exchange.composition._bind_live_ranking_snapshot` returns without binding a
   live reader whenever the document states that key, so re-adding it silently reverts the
   demo's `trust` column to a constant. Nothing else in the tree asserts this.
2. **the posture is REACHABLE** — every seeded Beta is the prior plus non-negative evidence.
   The version of `build_demo_deployment.py` that wrote a static snapshot emitted
   `Beta(13.02, 0.98)` for `gaiaherbs.com`'s dispatch record, and a `beta` below the prior's
   2.0 is not something any number of observations can produce. It was internally consistent
   and unreachable, and nothing noticed while the exchange read the document instead of the
   service.
3. **it is marked, permanently, in the hash chain, and it can be re-run** — every seeded event's
   `order_ref` carries the corpus's own `sim-fb-` prefix, read from
   `services/sim/seed/provenance.py` rather than retyped, `payload.simulated` is set on every
   one, ids and refs are unique, and — the half that was a measured defect — a run stamped with
   a different instant is a DIFFERENT event under the SAME id, which is why
   `seed_demo_trust.seeded_instant` reads the sealed instant back rather than reading the clock.
4. **it leaves the button somewhere to move** — `feedback_match` is seeded with nothing, and
   every other dimension carries enough POSITIVE rows to keep the store out of `low_data`.

Group 2 and group 4 are checked against the REAL scorer (`trust.scoring.score`) rather than
against this script's own arithmetic: the whole point is what the trust service will serve, and
two implementations of the same formula is exactly how a seed drifts from the snapshot it was
meant to produce.

Collection: `scripts` IS in `[tool.pytest.ini_options] testpaths`, so a bare `pytest` collects
this file. It opens no socket and needs no database.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = REPO_ROOT / "deploy" / "demo" / "exchange-deployment.json"


def _module(name: str) -> Any:
    """Load a `scripts/` module by path. They are scripts, not an installed package."""
    if str(REPO_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seeder() -> Any:
    return _module("seed_demo_trust")


@pytest.fixture(scope="module")
def generator() -> Any:
    return _module("build_demo_deployment")


# =====================================================================================
# 1. the deployment states no trust_snapshot
# =====================================================================================
def test_the_demo_deployment_states_no_trust_snapshot() -> None:
    """The one assertion that keeps the demo's trust column live.

    A stated `trust_snapshot` makes `exchange.composition._bind_live_ranking_snapshot` return
    early -- deliberately, and its docstring says why -- so the RANKING gate never reads the
    trust service. Measured on the version of this document that stated one: every published
    `trust` term was exactly `0.20 * <a number typed into this file>`, identical on every run of
    every auction, and no feedback a demo audience could give moved any of them.
    """
    document = json.loads(DEPLOYMENT.read_text(encoding="utf-8"))
    assert "trust_snapshot" not in document, (
        "deploy/demo/exchange-deployment.json states a trust_snapshot again. That key switches "
        "the exchange's ranking gate off the live trust service and back onto a frozen table; "
        "the demo's whole trust column becomes a constant. Seed the live service with "
        "`make demo-trust` instead."
    )
    assert document.get("trust_url"), (
        "with no stated snapshot the ranking gate resolves its reader from trust_url; without "
        "one it falls back to the ENV/default, which apps/exchange/compose.yaml does not set"
    )


def test_the_generator_does_not_write_the_key_back() -> None:
    """`--check` compares the generator's output to the tree, so both sides have to agree."""
    generator = _module("build_demo_deployment")
    built = generator.build()["exchange-deployment.json"]
    assert "trust_snapshot" not in built


# =====================================================================================
# 2. the posture is reachable
# =====================================================================================
def test_every_seeded_beta_is_the_prior_plus_non_negative_evidence(generator: Any) -> None:
    """No dimension asks for less than the prior. This is the `Beta(13.02, 0.98)` regression."""
    from trust.scoring.engine import PRIOR_ALPHA, PRIOR_BETA

    for store_id in sorted(generator.TRUST_SCORES):
        for dimension, target in generator.dimension_posteriors(store_id).items():
            assert float(target["alpha"]) >= PRIOR_ALPHA - 1e-9, (
                f"{store_id}/{dimension} states alpha {target['alpha']} below the prior "
                f"{PRIOR_ALPHA}; evidence only ever adds, so no run of observations reaches it"
            )
            assert float(target["beta"]) >= PRIOR_BETA - 1e-9, (
                f"{store_id}/{dimension} states beta {target['beta']} below the prior "
                f"{PRIOR_BETA}; evidence only ever adds, so no run of observations reaches it"
            )


def test_the_scripts_copy_of_the_prior_agrees_with_the_trust_engine(
    generator: Any, seeder: Any
) -> None:
    """Both scripts restate the prior because they run without a trust service. They must agree."""
    from trust.scoring.engine import PRIOR_ALPHA, PRIOR_BETA

    assert (generator.PRIOR_ALPHA, generator.PRIOR_BETA) == (PRIOR_ALPHA, PRIOR_BETA)
    assert (seeder.PRIOR_ALPHA, seeder.PRIOR_BETA) == (PRIOR_ALPHA, PRIOR_BETA)


def test_the_observation_types_the_seed_emits_carry_the_weights_it_assumes(seeder: Any) -> None:
    """A drift in the approved manifest would silently seed a different posture."""
    from trust.scoring.engine import OBSERVATION_POLARITY, OBSERVATION_WEIGHTS

    assert OBSERVATION_WEIGHTS[seeder.POSITIVE_TYPE] == 1.0
    assert OBSERVATION_WEIGHTS[seeder.CLAIM_POSITIVE_TYPE] == 1.0
    assert OBSERVATION_WEIGHTS[seeder.NEGATIVE_TYPE] == seeder.NEGATIVE_TYPE_WEIGHT
    assert OBSERVATION_POLARITY[seeder.POSITIVE_TYPE] == "positive"
    assert OBSERVATION_POLARITY[seeder.CLAIM_POSITIVE_TYPE] == "positive"
    assert OBSERVATION_POLARITY[seeder.NEGATIVE_TYPE] == "negative"


def test_the_real_scorer_reproduces_the_posture_the_seed_targets(
    seeder: Any, generator: Any
) -> None:
    """Fold the seeded events through `trust.scoring.score` and compare, dimension by dimension.

    This is the assertion that makes the whole script mean something: not "the arithmetic in
    this file is self-consistent" but "the trust engine, given these events, serves this."
    """
    from trust.ledger.replay import observations_from_events
    from trust.scoring import score

    as_of = "2026-09-08T12:00:00Z"
    events = list(seeder.seed_events(as_of))
    by_store: dict[str, list[dict[str, Any]]] = {}
    for observation in observations_from_events(events):
        by_store.setdefault(str(observation["store_id"]), []).append(observation)

    expected = seeder.expected_posture()
    for store_id, observations in sorted(by_store.items()):
        served = score(observations, as_of=as_of)
        for dimension, mean in expected[store_id].items():
            entry = served["dims"][dimension]
            actual = float(entry["alpha"]) / (float(entry["alpha"]) + float(entry["beta"]))
            assert actual == pytest.approx(mean, abs=1e-9), (
                f"{store_id}/{dimension}: the scorer folds the seeded events to {actual:.6f}, "
                f"the seed targets {mean:.6f}"
            )
        assert served["score"] == pytest.approx(
            sum(expected[store_id].values()) / len(expected[store_id]), abs=1e-9
        )


def test_the_seeded_order_is_the_stated_order(seeder: Any, generator: Any) -> None:
    """The demo's claim is who is more reliable. Compressing the range must not permute it.

    Over the TRANSACTING stores, which are the ones `TRUST_SCORES` states an order for. The
    crawled sellers are not in that table and are not in this comparison: they carry one
    dimension each and their `score` is five-sixths prior, so ranking them against a store with
    a transaction record would be comparing a posture to the absence of one.
    """
    posture = seeder.expected_posture()
    seeded = sorted(generator.TRUST_SCORES, key=lambda s: -sum(posture[s].values()))
    stated = sorted(generator.TRUST_SCORES, key=lambda s: -generator.TRUST_SCORES[s])
    assert seeded == stated


def test_a_crawled_seller_reads_as_unknown_rather_than_as_a_bad_transacting_one(
    seeder: Any, generator: Any
) -> None:
    """The nine promoted shops must sit at the prior on everything a transaction decides.

    This is the assertion that stops the crawl-only posture quietly becoming a full one. Give
    `sabai.design` a `shipped_on_time` entry in `DISPATCH_POSTERIOR` and this goes red, which
    is what should happen: nobody has ever ordered from it.
    """
    posture = seeder.expected_posture()
    neutral = seeder.PRIOR_ALPHA / (seeder.PRIOR_ALPHA + seeder.PRIOR_BETA)
    assert set(generator.CATALOGUE_ACCURACY) & set(generator.TRUST_SCORES) == set(), (
        "a store cannot be both crawled-only and transacting"
    )
    for store_id in sorted(generator.CATALOGUE_ACCURACY):
        means = posture[store_id]
        stated = {dim for dim, mean in means.items() if abs(mean - neutral) > 1e-9}
        assert stated == {"catalog_claim_accuracy"}, (
            f"{store_id} is a scraped shop and states a posture on {sorted(stated)}; the crawl "
            f"can only speak to catalog_claim_accuracy"
        )


# =====================================================================================
# 3. it is marked
# =====================================================================================
def test_every_seeded_event_is_marked_with_the_corpus_own_prefix(seeder: Any) -> None:
    """Read from `seed.provenance`, not retyped: two spellings of a marker is no marker."""
    sys.path.insert(0, str(REPO_ROOT / "services" / "sim"))
    from seed.provenance import SEED_MARKER_PREFIX

    assert seeder.SEED_MARKER_PREFIX == SEED_MARKER_PREFIX
    events = list(seeder.seed_events("2026-09-08T12:00:00Z"))
    assert events
    for event in events:
        assert str(event["order_ref"]).startswith(SEED_MARKER_PREFIX), event
        assert str(event["event_id"]).startswith(SEED_MARKER_PREFIX), event
        assert event["payload"]["simulated"] is True


def test_no_two_seeded_events_share_an_order_ref_or_an_event_id(seeder: Any) -> None:
    """`event_id` is the ledger's idempotency key; a collision would silently drop evidence.

    `order_ref` is separate and matters for its own reason: `trust.ledger.replay` discounts a
    positive report about an order the chain has already recorded a refund for, so one ref
    reused across a store's whole posture would make it one fictional order.
    """
    events = list(seeder.seed_events("2026-09-08T12:00:00Z"))
    assert len({e["event_id"] for e in events}) == len(events)
    assert len({e["order_ref"] for e in events}) == len(events)


def test_the_seed_is_the_same_events_every_run(seeder: Any) -> None:
    """Re-running must append nothing, which means the ids cannot depend on anything but inputs."""
    first = list(seeder.seed_events("2026-09-08T12:00:00Z"))
    second = list(seeder.seed_events("2026-09-08T12:00:00Z"))
    assert first == second


def test_a_different_instant_is_a_different_event_under_the_same_id(seeder: Any) -> None:
    """Why `seeded_instant` exists, stated as an assertion rather than left in a docstring.

    `event_id` is the ledger's idempotency key and re-sending an id with DIFFERENT content is a
    409 (D16), not a no-op. `observed_at` reaches both the event's `ts` and its
    `payload.observed_at`, so a second run that read the clock afresh would collide on its
    second event. Measured before the repair, by running `make demo-trust` twice:

        FATAL: POST /events answered 409 … already in the chain with DIFFERENT content

    This test is what keeps the two halves tied together: if `observed_at` ever stops reaching
    the event body, the repair becomes unnecessary and this goes red saying so.
    """
    early = {e["event_id"]: e for e in seeder.seed_events("2026-09-08T12:00:00.000Z")}
    late = {e["event_id"]: e for e in seeder.seed_events("2026-09-08T13:00:00.000Z")}
    assert set(early) == set(late), "the ids must not depend on the instant"
    differing = [key for key in early if early[key] != late[key]]
    assert len(differing) == len(early), (
        "every event must differ when the instant does; an event that did NOT would be one the "
        "chain could not tell apart, and the seed's timestamps would be unverifiable"
    )
    sample = early[differing[0]]
    assert sample["ts"] == sample["payload"]["observed_at"] == "2026-09-08T12:00:00.000Z"


# =====================================================================================
# 4. it leaves the button somewhere to move
# =====================================================================================
def test_the_whole_seed_stays_a_few_hundred_events(seeder: Any) -> None:
    """An upper bound on `DIMENSION_EVIDENCE`, which nothing else in this suite supplies.

    Found by mutation: raising `build_demo_deployment.DIMENSION_EVIDENCE` from 12 to 200 passed
    every other test in this file. The low-data floor only bounds it from BELOW, and the
    button-headroom test below is insensitive to it (the button's shift is fixed by
    `feedback_match` starting empty, and heavier evidence widens the gaps roughly in step). So
    a careless bump was a change nothing here would have caught.

    What it costs is not abstract. Every unit of POSITIVE evidence is one event, permanently, in
    an APPEND-ONLY chain with no delete — 200 would be roughly ten thousand of them, each an
    HTTP round trip and each triggering a store-agent notification. The bound is generous
    (12.0 produces 568) and exists to make a large jump a decision somebody takes on purpose.
    """
    events = list(seeder.seed_events("2026-09-08T12:00:00Z"))
    assert len(events) <= 1200, (
        f"the seed would write {len(events)} events into an append-only ledger. Something raised "
        f"build_demo_deployment.DIMENSION_EVIDENCE; if that is intended, raise this bound in the "
        f"same commit and say why."
    )
    assert len(events) >= 300, (
        f"the seed writes only {len(events)} events, which is fewer than ten stores x five "
        f"dimensions x the five positives the low-data floor needs. Something is being skipped."
    )


def test_every_demo_seller_is_seeded_on_every_floor_dimension(seeder: Any, generator: Any) -> None:
    """A store silently missing from the seed used to pass this whole file.

    Found by mutation: making `seed_events` skip `toniiq.com` left thirteen tests green,
    because both scorer tests iterate the stores the seed produced rather than the stores the
    demo has. A store the seed omits is a store the live snapshot has no row for, and R12
    excludes it from every auction — the exact failure this seed exists to prevent, arriving
    for one store instead of ten and therefore much harder to notice.
    """
    from trust.snapshot.builder import EPISODE_FLOOR_DIMENSIONS

    events = list(seeder.seed_events("2026-09-08T12:00:00Z"))
    seeded: dict[str, set[str]] = {}
    for event in events:
        seeded.setdefault(str(event["store_id"]), set()).add(str(event["payload"]["dim"]))

    assert set(seeded) == set(generator.DEMO_SELLERS), (
        f"the seed covers {sorted(seeded)} but the demo roster is "
        f"{sorted(generator.DEMO_SELLERS)}"
    )
    for store_id, dims in sorted(seeded.items()):
        if store_id in generator.CATALOGUE_ACCURACY:
            assert dims == {generator.CATALOG_DIMENSION}, (
                f"{store_id} is a crawled shop and is seeded on {sorted(dims)}; the crawl "
                f"produces catalogue evidence and nothing else"
            )
            continue
        assert dims == set(EPISODE_FLOOR_DIMENSIONS), (
            f"{store_id} is seeded on {sorted(dims)}, not on the five dimensions "
            f"clean_episodes takes its floor over ({sorted(EPISODE_FLOOR_DIMENSIONS)})"
        )


def test_every_host_in_the_demo_corpus_gets_a_trust_row(generator: Any) -> None:
    """R12 is fail-closed, so a corpus host with no seeded row is invisible on every shortlist.

    `blacklist_reason` excludes a store the live snapshot holds no row for, and `GET /snapshot`
    only knows the sellers this seed wrote. A storefront added to
    `fixtures/real-catalogs-demo/` and not to `DEMO_SELLERS` would load into the graph, be
    rostered by retrieval, and then be dropped from every ranking with no message anywhere —
    the symptom is a shorter shortlist. `build_demo_deployment.build` refuses that at build
    time; this is the same check where a reader will look for it.
    """
    missing = [host for host in generator.corpus_hosts() if host not in generator.DEMO_SELLERS]
    assert not missing, (
        f"{missing} are in fixtures/real-catalogs-demo and carry no trust posture, so R12 "
        f"excludes them from every shortlist"
    )


def test_the_scripts_copy_of_the_low_data_floor_agrees_with_the_trust_engine(seeder: Any) -> None:
    """`NEW_STORE_PRIOR_N` is `manifest_int(...)`, so it really can differ from 5.

    Found by mutation: setting the seeder's copy to 0 disarmed `observations_for`'s low-data
    refusal and every test still passed. The prior's `(2, 2)` is pinned against the engine two
    tests up; this is the same argument for the same reason.
    """
    from trust.scoring.engine import NEW_STORE_PRIOR_N

    assert seeder.NEW_STORE_PRIOR_N == NEW_STORE_PRIOR_N


def test_each_dimension_rides_the_event_kind_that_really_carries_it(seeder: Any) -> None:
    """The seed may manufacture evidence; it may not manufacture the pairing.

    `claim_verified` for the two dimensions `trust.scoring.dimensions.CLAIM_TYPE_DIMENSIONS`
    routes claims onto, `reconciled` for exactly the three
    `trust.reconcile.engine.RECONCILED_DIMENSIONS` grades. An earlier version put
    `not_returned` on `reconciled` and justified it by saying that dimension had no producer —
    which was wrong twice over, and this is the assertion that keeps it right.
    """
    from trust.reconcile.engine import RECONCILED_DIMENSIONS
    from trust.scoring.dimensions import CLAIM_TYPE_DIMENSIONS

    claim_backed = set(CLAIM_TYPE_DIMENSIONS.values())
    reconciled_dims = {str(dim) for dim in RECONCILED_DIMENSIONS.values()}
    assert seeder.CLAIM_VERIFIED_DIMENSIONS <= claim_backed
    assert not (seeder.CLAIM_VERIFIED_DIMENSIONS & reconciled_dims)

    by_kind: dict[str, set[str]] = {}
    for event in seeder.seed_events("2026-09-08T12:00:00Z"):
        by_kind.setdefault(str(event["kind"]), set()).add(str(event["payload"]["dim"]))
    assert by_kind["claim_verified"] == set(seeder.CLAIM_VERIFIED_DIMENSIONS)
    assert by_kind["reconciled"] == reconciled_dims


def test_a_reconciled_seed_event_never_states_a_dishonoured_price_it_does_not_mean(
    seeder: Any,
) -> None:
    """`reconciled`'s published shape requires two booleans; they must say something true.

    An earlier version set BOTH from the polarity of whatever dimension the event was about,
    so every seeded "shipped late" sealed `price_honored: false` into an append-only chain —
    an assertion about the store's pricing that nothing in the seed meant to make.
    """
    for event in seeder.seed_events("2026-09-08T12:00:00Z"):
        if event["kind"] != "reconciled":
            continue
        payload = event["payload"]
        negative = payload["type"] == seeder.NEGATIVE_TYPE
        for key in ("price_honored", "discount_honored"):
            expected = (not negative) if payload["dim"] == key else True
            assert payload[key] is expected, (
                f"{event['event_id']}: dim={payload['dim']} type={payload['type']} states "
                f"{key}={payload[key]}"
            )


def test_verify_fails_on_a_missing_store_and_not_on_earned_evidence(
    seeder: Any, monkeypatch: Any
) -> None:
    """The pass/fail decision itself, which nothing else in this file exercises.

    Both directions matter and the second one is a measured defect: an earlier `verify` failed
    when a served mean crossed neutral away from its seeded side, and TWO on-time deliveries
    flip `toniiq.com/shipped_on_time` — so the most ordinary event in the system would have
    turned `make demo-trust` permanently red.
    """
    posture = seeder.expected_posture()

    def snapshot(rows: dict[str, Any]) -> Any:
        return lambda url, body=None, timeout=0.0: (200, rows)

    healthy = {
        store: {
            "score": sum(means.values()) / len(means),
            "blacklisted": False,
            "low_data": False,
            "dims": {
                dim: {"alpha": 2.0 + 12.0 * mean, "beta": 2.0 + 12.0 * (1.0 - mean)}
                for dim, mean in means.items()
            },
        }
        for store, means in posture.items()
    }
    monkeypatch.setattr(seeder, "_http", snapshot(healthy))
    problems, _drift = seeder.verify("http://trust.invalid")
    assert problems == []

    # A store trust has never heard of: R12 excludes it, so this must be a failure.
    missing = {k: v for k, v in healthy.items() if k != "toniiq.com"}
    monkeypatch.setattr(seeder, "_http", snapshot(missing))
    problems, _drift = seeder.verify("http://trust.invalid")
    assert any("toniiq.com" in p and "no row" in p for p in problems), problems

    # toniiq ships on time twice: its worst dimension crosses neutral. Honest traffic, and the
    # seed is still perfectly usable, so this is DRIFT and never a problem.
    earned = {k: {**v, "dims": dict(v["dims"])} for k, v in healthy.items()}
    earned["toniiq.com"]["dims"]["shipped_on_time"] = {"alpha": 12.0, "beta": 8.6}
    monkeypatch.setattr(seeder, "_http", snapshot(earned))
    problems, drift = seeder.verify("http://trust.invalid")
    assert problems == [], problems
    assert any("toniiq.com/shipped_on_time" in d for d in drift), drift

    # Blacklisted and low_data are structural: the exchange cannot rank the store either way.
    for flag in ("blacklisted", "low_data"):
        broken = {k: {**v} for k, v in healthy.items()}
        broken["purebulk.com"] = {**broken["purebulk.com"], flag: True}
        monkeypatch.setattr(seeder, "_http", snapshot(broken))
        problems, _drift = seeder.verify("http://trust.invalid")
        assert any("purebulk.com" in p for p in problems), (flag, problems)


def test_feedback_match_is_never_seeded(seeder: Any) -> None:
    """The one dimension `POST /buyer/feedback` can move starts empty, or the button is damped."""
    for event in seeder.seed_events("2026-09-08T12:00:00Z"):
        assert event["payload"]["dim"] != "feedback_match", event


def test_no_transacting_store_is_served_low_data(seeder: Any, generator: Any) -> None:
    """`low_data` switches on the exchange's exploration floor, a second randomised mechanism.

    Checked through `trust.snapshot.builder`'s own `clean_episodes` and its own floor-dimension
    set, not by counting rows here: the rule about which dimensions the floor is taken over is
    that module's, and a copy of it in this file would be free to drift.
    """
    from trust.ledger.replay import observations_from_events
    from trust.snapshot.builder import NEW_STORE_PRIOR_N, clean_episodes

    events = list(seeder.seed_events("2026-09-08T12:00:00Z"))
    by_store: dict[str, list[dict[str, Any]]] = {}
    for observation in observations_from_events(events):
        by_store.setdefault(str(observation["store_id"]), []).append(observation)

    for store_id, observations in sorted(by_store.items()):
        episodes = clean_episodes({"store_id": store_id}, observations)
        if store_id in generator.CATALOGUE_ACCURACY:
            # DELIBERATELY low_data, and asserted in that direction. A crawled shop has no
            # completed episode with anybody; `low_data` is `trust.snapshot.builder`'s own
            # "unknown rather than average", which is the true thing to say about it. If this
            # ever came out >= the floor, the seed would have manufactured a transaction
            # record for a shop nobody has bought from.
            assert episodes == 0, (
                f"{store_id} is a crawled shop and derives {episodes} clean episodes; the seed "
                f"writes catalogue evidence only, so this must be 0"
            )
            continue
        assert episodes >= NEW_STORE_PRIOR_N, (
            f"{store_id} derives {episodes} clean episodes from the seed and would be served "
            f"low_data (< {NEW_STORE_PRIOR_N})"
        )


def test_one_press_of_the_button_outruns_the_gaps_between_the_stores_it_reaches(
    seeder: Any, generator: Any
) -> None:
    """The seed is only useful if the demo can move it. Two claims, and the second is the real one.

    The learning page feeds a store `OUTCOME_ROUNDS` (6) x `REVIEWERS_PER_ROUND` (4) = 24
    `feedback` events, every one landing on `feedback_match` and nothing else
    (`trust.feedback.engine.FEEDBACK_DIMENSION`), and this folds them through the real scorer.

    **The ceiling** — all 24 positive — is +0.0714 on the published `score`. That is the most a
    press can ever do, and it is compared against the widest gap in the whole roster.

    **The realistic case** matters more, and an earlier version of this test did not have it.
    `apps/buyer/app/learning/loop.ts` sets each store's verdict from whether it offered a
    discount THAT ROUND, so the 24 are a mix. Measured on the served route, one press:
    `oregonswildharvest.com` came out 20 positive / 4 negative and moved +0.038888;
    `paradiseherbs.com` came out 16/8 and moved +0.010416. So a 16/8 press does NOT clear every
    gap, and the demo's claim has to rest on the mix a press actually produces against the
    stores it actually reaches — the four with hosted agents, which are the only ones that bid
    and therefore the only ones that receive feedback at all.
    """
    from trust.scoring import score

    as_of = "2026-09-08T12:00:00Z"

    def shift(positive: int, negative: int) -> float:
        rows = [{"dim": "feedback_match", "type": "fulfilled", "observed_at": as_of}] * positive
        rows += [
            {"dim": "feedback_match", "type": "mismatch_return", "observed_at": as_of}
        ] * negative
        moved = score(rows, as_of=as_of)["dims"]["feedback_match"]
        mean = float(moved["alpha"]) / (float(moved["alpha"]) + float(moved["beta"]))
        return (mean - 0.5) / 6.0

    posture = seeder.expected_posture()
    scores = {store: sum(m.values()) / len(m) for store, m in posture.items()}

    # Over the TRANSACTING ten. The crawled nine sit as a block below every one of them —
    # `test_a_crawled_seller_ranks_below_every_transacting_one` pins that — and the step down
    # to that block is wider than any press, which is correct rather than a defect: a press
    # lands on `feedback_match`, and no amount of buyer feedback should promote a shop the
    # platform has never watched deliver past one it has.
    transacting = sorted((scores[store] for store in generator.TRUST_SCORES), reverse=True)
    widest = max(a - b for a, b in zip(transacting, transacting[1:], strict=False))
    assert shift(24, 0) > widest, (
        f"even 24 straight positives move a store's published score by only {shift(24, 0):.4f}, "
        f"and the widest gap between two adjacent transacting stores is {widest:.4f}. Nothing a "
        f"press can do would reorder them; the seed is too heavy or too spread out."
    )

    hosted = sorted((scores[store] for store in generator.HOSTED), reverse=True)
    widest_hosted = max(a - b for a, b in zip(hosted, hosted[1:], strict=False))
    assert shift(20, 4) > widest_hosted, (
        f"the mix one press actually produced on the served route (20 positive, 4 negative) "
        f"moves a store by {shift(20, 4):.4f}, and the widest gap between two adjacent HOSTED "
        f"stores — the four that bid, and so the only four a press reaches — is "
        f"{widest_hosted:.4f}. A press would not reorder the shortlist."
    )


def test_a_crawled_seller_ranks_below_every_transacting_one(seeder: Any, generator: Any) -> None:
    """The two postures are meant to be visibly different, and this is where that is stated.

    A crawled shop's `score` is five-sixths neutral prior and one-sixth catalogue evidence, so
    it lands just above 0.5; a transacting store carries evidence on five of the six. If a
    crawled shop ever outranked a transacting one on `score`, the demo would be saying the
    platform trusts a shop it has only read more than one it has traded with.

    It is NOT a claim about shortlists. `trust` is one weighted term of several and a crawled
    shop can and should still win a shortlist slot on relevance, price or delivery — which is
    exactly what happens on the furniture queries this roster was widened for.
    """
    posture = seeder.expected_posture()
    scores = {store: sum(m.values()) / len(m) for store, m in posture.items()}
    floor = min(scores[store] for store in generator.TRUST_SCORES)
    for store_id in sorted(generator.CATALOGUE_ACCURACY):
        assert scores[store_id] < floor, (
            f"{store_id} is crawled-only and scores {scores[store_id]:.4f}, at or above the "
            f"weakest transacting store ({floor:.4f})"
        )


def test_a_reseed_after_the_roster_grows_finds_the_instant_the_chain_already_holds(
    seeder: Any, generator: Any
) -> None:
    """The 409 that an append-only ledger has no repair for. Measured, then fixed here.

    `event_id` is the ledger's idempotency key and re-sending an id with DIFFERENT content is
    refused (D16), so a re-run MUST reuse the `observed_at` the chain already carries.
    `seed_demo_trust.main` finds it by asking `seeded_instant` about a handful of event ids;
    the ids it asks about used to be the FIRST EIGHT events of the run, which is the first two
    stores in `sorted(DEMO_SELLERS)` order.

    Promoting `branchfurniture.com` puts a new store alphabetically first. Every one of those
    eight probes then names an event no previous run wrote, all eight miss, the clock wins, and
    the next event for a store that IS on the chain comes back 409 — the demo's trust seed
    permanently unrunnable without tearing the stack down.

    So the probe set must contain, for every store, an id a run that seeded THAT store would
    have written. This asserts exactly that, against the ten-store roster the chain in the wild
    was seeded from.
    """
    probes = {
        str(event["event_id"])
        for event in seeder.seed_events("", sorted(generator.DEMO_SELLERS))
        if str(event["event_id"]).endswith("-00")
    }
    incumbent_first_events = {}
    for event in seeder.seed_events("", sorted(generator.TRUST_SCORES)):
        incumbent_first_events.setdefault(str(event["store_id"]), str(event["event_id"]))
    unreachable = sorted(set(incumbent_first_events.values()) - probes)
    assert not unreachable, (
        f"a re-seed would not ask about {unreachable}, so a chain seeded before the roster grew "
        f"would go undetected and the run would stamp a fresh instant and 409"
    )

    first_of_each: dict[str, str] = {}
    for event in seeder.seed_events("", sorted(generator.DEMO_SELLERS)):
        first_of_each.setdefault(str(event["store_id"]), str(event["event_id"]))
    assert set(first_of_each) == set(generator.DEMO_SELLERS), (
        "the probe set must cover every seller, not the first few in sort order"
    )
