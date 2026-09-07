"""The seeding harness as a script, its stored artifact, and the switch to live customers.

Four groups, and they are the owner's three requirements plus the one-way door under the
third:

1. **it is a script, not a service** — the simulation image must ship nothing it cannot run,
   which is a property of where these modules live rather than of a list someone maintains.
2. **run it, store it, replay it** — a stored artifact must reproduce the postures the served
   routes actually returned, offline, deterministically, without re-simulating anything.
3. **seeded and real must stay distinguishable** — permanently, on an append-only chain, and
   a reader must be able to compute a store's posture with the manufactured observations and
   without them.
4. **the live switch** — the served route must not be able to tell a seeded caller from a real
   one, because that is what makes "stop running the seeder" the whole of going live.

Offline (D3/C9): every URL here is a loopback port the test itself bound, and the replay path
is asserted to work with :func:`socket.socket` sabotaged outright.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# ------------------------------------------------------------------------------------
# 1. It is a script, not a service
# ------------------------------------------------------------------------------------
#: The modules that make up the seeding harness. They import ``buyer_svc``,
#: ``exchange.ranking``, ``claim_verification`` and ``llm``; the simulation image carries none
#: of those, which is why none of these may live in the directory that image copies.
HARNESS_MODULES = ("population.py", "shoppers.py", "targets.py", "local_stack.py")


def test_the_harness_lives_outside_the_directory_the_simulation_image_copies(
    seedfx_repo_root: Path,
) -> None:
    """``services/sim/src/`` must hold only what ``python -m sim`` can run.

    The regression this pins is not hypothetical: while these four modules sat in
    ``services/sim/src/``, ``COPY services/sim/src/`` put them into an image that cannot
    import ``buyer_svc`` or ``exchange.ranking``, and
    ``test_t301_every_image_copy_set_covers_every_first_party_import_it_ships`` was red on
    exactly that line. Asserting the LOCATION rather than the Dockerfile's contents is
    deliberate — a copy set is a list somebody has to remember, and a directory boundary is
    not.
    """
    shipped = seedfx_repo_root / "services" / "sim" / "src"
    harness = seedfx_repo_root / "services" / "sim" / "seed"
    for name in HARNESS_MODULES:
        assert not (shipped / name).exists(), (
            f"{shipped / name} is inside the directory `COPY services/sim/src/` ships, and the "
            f"simulation image cannot import what it needs. It belongs in {harness}."
        )
        assert (harness / name).is_file(), f"{harness / name} is missing"


def test_the_simulation_image_copies_nothing_from_the_seeding_harness(
    seedfx_repo_root: Path,
) -> None:
    """No ``COPY`` in the simulation Dockerfile may reach ``services/sim/seed/``.

    The other half of the boundary above, read off the artifact's own description. A future
    edit that "helpfully" adds the harness to the image would make T-301 red again by way of
    five more subtrees; this says so at the line that would do it.
    """
    text = (seedfx_repo_root / "services" / "sim" / "Dockerfile").read_text(encoding="utf-8")
    sources = [
        line.split()[1]
        for line in text.splitlines()
        if line.startswith("COPY ")
        and len(line.split()) >= 3
        and not line.split()[1].startswith("--")
    ]
    offenders = [source for source in sources if source.rstrip("/").startswith("services/sim/seed")]
    assert offenders == [], (
        f"services/sim/Dockerfile copies {offenders} into an image whose CMD is `python -m sim`. "
        "The harness needs buyer_svc, claim_verification, llm and exchange.ranking; shipping it "
        "reverses T-310's narrowing and drags the ingest graph library into an image that opens "
        "no socket."
    )
    assert "COPY services/sim/src/" in text, (
        "the simulation image must still copy its own source directory whole"
    )


def test_the_simulator_does_not_import_the_seeding_harness(seedfx_repo_root: Path) -> None:
    """``seed`` imports ``sim``; ``sim`` must never import ``seed``.

    A one-way edge is what keeps the image's closure a straight line from its ``CMD``. If it
    ever reversed, ``services/sim/src/`` would be unable to import inside the container and
    the boundary above would be true and useless.
    """
    import ast

    shipped = seedfx_repo_root / "services" / "sim" / "src"
    offenders: list[str] = []
    for module in sorted(shipped.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            # Every import site, at module scope or indented. An indented one is the dangerous
            # kind here: it survives `image_import_report`'s probe (nothing imports it at load
            # time) and dies the first time the container reaches that line.
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "seed" or name.startswith("seed.") for name in names):
                offenders.append(f"{module.name}:{node.lineno}")
    assert offenders == [], (
        f"{offenders} import the seeding harness. The simulation image ships these modules and "
        "does not ship `seed`, so the import is dead inside the container — and an indented one "
        "would be dead silently, at the first line that reaches it."
    )


# ------------------------------------------------------------------------------------
# 2. Run it, store it, replay it
# ------------------------------------------------------------------------------------
def test_the_offline_projection_reproduces_what_the_served_snapshot_returned(
    pop_served: tuple[Any, Any],
    pop_roster: list[dict[str, Any]],
) -> None:
    """The replay's arithmetic must be the trust engine's, not a second copy of it.

    This is the assertion the whole store-and-replay path rests on. :func:`seed.provenance.
    postures` projects a ledger chain into postures without a socket; ``GET /snapshot``
    projects the same chain into postures with one. If those two ever disagree, every number
    the stored artifact reports is a number no served route would have produced — and a
    replay that quietly used its own scorer would look exactly like a replay that worked.
    """
    from seed.population import posture_view
    from seed.provenance import as_of_of, postures

    run, services = pop_served
    projected = postures(
        list(services.event_store.read()),
        pop_roster,
        as_of=as_of_of(run.closing_snapshot),
    )
    assert posture_view(projected) == posture_view(run.closing_snapshot)


def test_a_stored_artifact_replays_to_the_postures_the_routes_served(
    seedfx_artifact: Any,
) -> None:
    """Load the artifact, recompute from its chain, and get back what the run served.

    Byte-for-byte on the canonical form, not "close enough": the comparison is over the same
    JSON serialisation the transcript digest is taken from, so a difference in the sixth
    decimal place of one dimension of one store fails this.
    """
    from seed.population import posture_view
    from seed.store import canonical_bytes, replay_postures

    replayed = posture_view(replay_postures(seedfx_artifact))
    stored = seedfx_artifact.closing_postures
    assert canonical_bytes(replayed) == canonical_bytes(stored)


def test_replaying_the_same_artifact_twice_returns_the_same_bytes(seedfx_artifact: Any) -> None:
    """A replay decays against the stored ``as_of``, never a clock, so it cannot drift.

    The population already proved byte-identical runs across two processes. This is that
    property carried through the store-and-replay path: the artifact is the same bytes, so
    the answer computed from it must be too, however long after the run it is asked for.
    """
    from seed.population import posture_view
    from seed.store import canonical_bytes, replay_postures

    first = canonical_bytes(posture_view(replay_postures(seedfx_artifact)))
    second = canonical_bytes(posture_view(replay_postures(seedfx_artifact)))
    assert first == second


def test_a_replay_opens_no_socket_at_all(seedfx_artifact: Any, monkeypatch: Any) -> None:
    """Sabotage :func:`socket.socket` outright; the replay must still produce its postures.

    Stronger than trusting the session's socket guard, and stronger than reading the source:
    the guard permits loopback (the population needs it), so a replay that quietly stood a
    service up would pass under it. With the constructor itself raising, a replay that touched
    any service — even one on 127.0.0.1 — cannot come back green.
    """
    import socket

    from seed.store import replay_postures

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a replay must not open a socket: nothing is re-simulated")

    monkeypatch.setattr(socket, "socket", _refuse)
    postures = replay_postures(seedfx_artifact)
    assert postures, "the replay produced no postures"


def test_the_stored_transcript_is_exactly_what_the_run_digests(seedfx_artifact: Any) -> None:
    """``transcript.json`` must hold the canonical bytes ``transcript_digest`` hashes.

    That equality is what makes ``determinism.transcript_digest`` an instruction rather than
    decoration: a reader can run the recorded ``producer`` command, digest the transcript it
    gets, and compare with the recorded value without knowing anything about this format.
    """
    import hashlib

    from seed.store import TRANSCRIPT_FILE, canonical_bytes

    body = (seedfx_artifact.root / TRANSCRIPT_FILE).read_bytes()
    assert body == canonical_bytes(seedfx_artifact.transcript) + b"\n"
    recorded = seedfx_artifact.collection["determinism"]["transcript_digest"]
    assert hashlib.sha256(body.rstrip(b"\n")).hexdigest() == recorded


def test_the_stored_chain_is_the_chain_the_routes_wrote(
    seedfx_artifact: Any, pop_served: tuple[Any, Any]
) -> None:
    """``ledger.jsonl`` is the trust service's own events, not this package's account of them.

    A transcript is the seeder's story; the chain is the platform's. Storing only the first
    would leave a reader with nothing to check the seeder against, which is precisely the
    position an audit must not be in.
    """
    _, services = pop_served
    written = [dict(event) for event in services.event_store.read()]
    assert list(seedfx_artifact.events) == written
    for event in seedfx_artifact.events:
        assert event["kind"] == "feedback"
        assert event["event_hash"] and event["prev_hash"]


def test_a_tampered_artifact_is_refused_rather_than_loaded(
    seedfx_artifact: Any, tmp_path: Path
) -> None:
    """A corpus whose bytes and whose provenance record disagree must not load.

    "Looks accounted for" is worse than "not accounted for": a digest nobody checks is a
    digest that launders a change. The refusal names the file and both hashes.
    """
    import shutil

    from seed.store import TRANSCRIPT_FILE, SeedArtifactError, load

    copy = tmp_path / "tampered"
    shutil.copytree(seedfx_artifact.root, copy)
    body = json.loads((copy / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    body["seed"] = int(body["seed"]) + 1
    (copy / TRANSCRIPT_FILE).write_text(json.dumps(body, sort_keys=True), encoding="utf-8")

    with pytest.raises(SeedArtifactError) as caught:
        load(copy)
    assert TRANSCRIPT_FILE in str(caught.value)
    assert "digest" in str(caught.value)


def test_the_checked_in_seed_corpus_loads_and_replays() -> None:
    """The artifact stored in this repository must itself be loadable and self-consistent.

    Not a duplicate of the tests above, and this is the distinction that matters: those run
    against a corpus this session just produced, which is the one case where the writer and
    the reader are guaranteed to agree. This one runs against the bytes actually committed —
    the ones a demo will read — and would catch a corpus written by an older layout, a
    partial write, or a hand edit.
    """
    from seed.population import posture_view
    from seed.store import SeedArtifactError, canonical_bytes, default_root, load, replay_postures

    if not (default_root() / "collection.json").is_file():
        pytest.skip(f"no seed corpus stored at {default_root()}; run `python -m seed run`")
    try:
        artifact = load()
    except SeedArtifactError as exc:  # pragma: no cover - the failure is the message
        pytest.fail(str(exc))
    replayed = canonical_bytes(posture_view(replay_postures(artifact)))
    assert replayed == canonical_bytes(artifact.closing_postures)


def test_the_replay_subcommand_exits_zero_on_a_good_artifact(
    seedfx_artifact: Any, capsys: Any
) -> None:
    """``python -m seed replay`` is the command a demo runs; its status is the finding."""
    from seed.__main__ import main

    assert main(["replay", "--from", str(seedfx_artifact.root)]) == 0
    printed = capsys.readouterr().out
    assert "SIMULATED" in printed
    assert "MATCHES" in printed


def test_the_replay_subcommand_reports_a_chain_that_does_not_match(
    seedfx_artifact: Any, tmp_path: Path
) -> None:
    """Drop an event from the stored chain and the replay must refuse to call it a match.

    The falsification for the test above. A replay that loaded the transcript's postures and
    printed them would pass every assertion in this file except this one — so this is the one
    that establishes the replay actually recomputes from the chain.
    """
    import hashlib
    import shutil

    from seed.__main__ import main
    from seed.store import COLLECTION_FILE, LEDGER_FILE

    copy = tmp_path / "short-chain"
    shutil.copytree(seedfx_artifact.root, copy)
    lines = (copy / LEDGER_FILE).read_bytes().splitlines(keepends=True)
    truncated = b"".join(lines[:-1])
    (copy / LEDGER_FILE).write_bytes(truncated)
    record = json.loads((copy / COLLECTION_FILE).read_text(encoding="utf-8"))
    record["file_sha256"]["ledger"] = hashlib.sha256(truncated).hexdigest()
    (copy / COLLECTION_FILE).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    assert main(["replay", "--from", str(copy)]) == 1


# ------------------------------------------------------------------------------------
# 3. Seeded and real, distinguishable forever
# ------------------------------------------------------------------------------------
def test_every_stored_observation_carries_the_simulated_marker(seedfx_artifact: Any) -> None:
    """Not one event in a seeded corpus may be unmarked.

    The marker is on ``order_ref``, which the buyer service copies verbatim onto the sealed
    event, so this reads the value that is actually inside the hash chain rather than a flag
    this package attached afterwards.
    """
    from seed.provenance import SEED_MARKER_PREFIX, is_seeded_event

    assert seedfx_artifact.events, "the corpus stored no events"
    unmarked = [event["event_id"] for event in seedfx_artifact.events if not is_seeded_event(event)]
    assert unmarked == [], (
        f"{len(unmarked)} manufactured observation(s) reached an append-only ledger without the "
        f"{SEED_MARKER_PREFIX!r} marker. There is no delete; an unmarked one is permanent."
    )
    assert seedfx_artifact.collection["marker"]["organic_events"] == 0


def test_a_store_posture_is_computable_with_and_without_the_seeded_observations(
    seedfx_artifact: Any,
) -> None:
    """The audit the marker exists for: manufactured reputation, separable from earned.

    Every store this corpus seeded must come back with two numbers and the difference between
    them. The "without" pass is not a store *disappearing* — a store whose only observations
    were seeded is served at its low-data prior, because "has no earned reputation" and "does
    not exist" are different sentences.
    """
    from seed.provenance import posture_split

    split = posture_split(
        seedfx_artifact.events, seedfx_artifact.roster, as_of=seedfx_artifact.as_of
    )
    assert set(split) == {str(row["store_id"]) for row in seedfx_artifact.roster}

    moved = [row for row in split.values() if row.seeded_observations]
    assert moved, "this corpus seeded nothing, so there is nothing to audit"
    for row in moved:
        assert row.without_seed == pytest.approx(0.5, abs=1e-9), (
            f"{row.store_id} has no organic observations, so without the seed it must sit at "
            f"the prior; it computed {row.without_seed}"
        )
        assert row.with_seed != pytest.approx(row.without_seed, abs=1e-9), (
            f"{row.store_id} carries {row.seeded_observations} manufactured observation(s) and "
            "its posture is identical with and without them — which would mean the seeded "
            "feedback moved nothing and the marker is separating nothing"
        )
        assert row.attributable_to_seed == pytest.approx(row.with_seed - row.without_seed)


def test_an_unmarked_observation_is_counted_as_earned(seedfx_artifact: Any) -> None:
    """A real customer's answer must survive the "without the seed" pass. Both halves matter.

    The falsification for the test above, and the case the platform will actually be in the
    day after going live: a chain carrying both kinds. An implementation that filtered on
    anything but the marker — the event's kind, its store, its position in the chain — passes
    every other test here and fails this one.
    """
    from seed.provenance import is_seeded_event, observation_census, posture_split

    seeded = dict(seedfx_artifact.events[0])
    store_id = str(seeded["store_id"])
    organic = {
        **seeded,
        "event_id": "fb-earned-0000000000000000000000000000",
        "order_ref": "ord-e7-101",
        # `fulfilled` is the positive `feedback_match` type the buyer service writes for a
        # "yes, as described" answer — a published weight in the approved manifest, not a
        # value invented here. An unknown one would be refused by the scorer, which is the
        # correct behaviour and would make this test about the wrong thing.
        "payload": {**seeded["payload"], "type": "fulfilled", "matched_pitch": True},
    }
    assert not is_seeded_event(organic)

    chain = [*seedfx_artifact.events, organic]
    census = observation_census(chain)
    assert census[store_id]["organic"] == 1

    split = posture_split(chain, seedfx_artifact.roster, as_of=seedfx_artifact.as_of)
    assert split[store_id].organic_observations == 1
    assert split[store_id].without_seed > 0.5, (
        "one positive earned observation must lift this store above the prior in the "
        "seed-excluded pass; it did not, so the pass is not reading the earned events"
    )


def test_the_marker_cannot_be_forged_by_a_payload_field(seedfx_artifact: Any) -> None:
    """Only the reference decides, and the reference is what the chain seals.

    An event whose *payload* claims to be simulated while its ``order_ref`` is a real one must
    still count as earned. The distinction is not pedantry: the payload is built by the buyer
    service from the caller's body, so a marker read out of it would be a claim any caller
    could make about somebody else's order.
    """
    from seed.provenance import is_seeded_event

    real = dict(seedfx_artifact.events[0])
    real["order_ref"] = "ord-e7-101"
    real["payload"] = {**real["payload"], "simulated": True, "source": "sim-fb-"}
    assert not is_seeded_event(real)


# ------------------------------------------------------------------------------------
# 4. The live switch
# ------------------------------------------------------------------------------------
def test_the_served_feedback_route_has_no_notion_of_simulation(seedfx_repo_root: Path) -> None:
    """``apps/buyer/svc/src/feedback/`` must not mention simulation, seeding or this package.

    This is what makes "stop running the seeder" the whole of going live. A single branch on
    the caller — a header, a prefix check, a debug flag — would mean the production path and
    the seeded path are two paths, and everything proven about one would stop being evidence
    about the other.
    """
    feedback = seedfx_repo_root / "apps" / "buyer" / "svc" / "src" / "feedback"
    offenders: dict[str, list[str]] = {}
    for module in sorted(feedback.rglob("*.py")):
        hits = [
            f"{module.name}:{number}"
            for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1)
            if "simulat" in line.lower() or "seed" in line.lower()
        ]
        if hits:
            offenders[module.name] = hits
    assert offenders == {}, (
        f"the served feedback path now knows what a simulator is: {offenders}. The seam is that "
        "it does not; a route that can tell a seeded caller from a real one is a route with two "
        "behaviours, and going live stops being a one-line change."
    )


def test_the_route_cannot_tell_a_seeded_caller_from_a_real_one(seedfx_stack: Any) -> None:
    """Two submissions, identical but for the reference. The route must treat them identically.

    Driven through the real ``POST /buyer/feedback/prompt`` and ``POST /buyer/feedback`` on a
    live buyer service wired to a live trust service — the same doors the population uses and
    the same doors a shopper's browser would. What is asserted is that the status, the response
    body and the sealed ledger event differ **only** in the identifiers, and that the marker is
    the one thing that separates them afterwards.
    """
    import httpx
    from seed.provenance import is_seeded_event

    order = {
        "store_id": "store-brightbean",
        "auction_id": "sim-fb-a99-00",
        "routed": True,
        "status": "delivered",
    }
    answer = {"question_id": "matched_pitch", "choice": "yes_as_described"}
    volatile = {"event_id", "ts", "order_ref", "seq", "event_hash", "prev_hash"}

    with httpx.Client(timeout=30.0) as client:
        bodies: dict[str, dict[str, Any]] = {}
        prompts: dict[str, dict[str, Any]] = {}
        for label, order_ref in (
            ("seeded", "sim-fb-99-00-store-brightbean"),
            ("real", "ord-99-00"),
        ):
            payload = {**order, "order_ref": order_ref}
            prompt = client.post(
                f"{seedfx_stack.buyer_url}/buyer/feedback/prompt", json={"order": payload}
            )
            assert prompt.status_code == 200, prompt.text
            prompts[label] = prompt.json()
            submitted = client.post(
                f"{seedfx_stack.buyer_url}/buyer/feedback",
                json={"order": payload, "response": answer},
            )
            assert submitted.status_code == 201, submitted.text
            bodies[label] = submitted.json()

    def _stripped(body: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in body.items() if key not in volatile}

    assert _stripped(prompts["seeded"]["prompt"]) == _stripped(prompts["real"]["prompt"])
    assert _stripped(bodies["seeded"]) == _stripped(bodies["real"])

    chain = [
        event
        for event in seedfx_stack.event_store.read()
        if str(event.get("order_ref")) in {"sim-fb-99-00-store-brightbean", "ord-99-00"}
    ]
    assert len(chain) == 2
    sealed = {str(event["order_ref"]): dict(event) for event in chain}
    assert _stripped(sealed["sim-fb-99-00-store-brightbean"]) == _stripped(sealed["ord-99-00"])

    # Indistinguishable to the route, and permanently distinguishable to a reader. Both halves
    # are the requirement; either one alone is the wrong system.
    assert is_seeded_event(sealed["sim-fb-99-00-store-brightbean"])
    assert not is_seeded_event(sealed["ord-99-00"])


def test_the_buyer_client_already_speaks_to_exactly_these_two_routes(
    seedfx_repo_root: Path,
) -> None:
    """The browser-side half of the switch: the client exists and points at the same doors.

    Asserted rather than assumed because the claim "going live needs no server change" is only
    useful if something on the other side is ready to call the same route. It is:
    ``apps/buyer/app/feedback/feedback.ts`` declares both paths as constants and posts to them.

    What this test deliberately does NOT assert is that the prompt is reachable in the running
    app — it is not. ``FeedbackPromptView`` is rendered by no page; measured, its only importer
    in the whole app is its own test file. That gap lives in ``apps/buyer/app`` rather than
    here, and stating it is the point: "the UI exists" and "a shopper can reach it" are
    different claims and only the first one is true today.
    """
    client = seedfx_repo_root / "apps" / "buyer" / "app" / "feedback" / "feedback.ts"
    text = client.read_text(encoding="utf-8")
    assert "'/buyer/feedback/prompt'" in text
    assert "'/buyer/feedback'" in text


def test_the_population_refuses_a_non_loopback_target_without_the_flag(
    pop_manifest: dict[str, Any],
) -> None:
    """The loopback guard survives the move. It manufactures reputation; it stays local.

    Re-asserted here rather than left to ``test_feedback_population`` because this file is
    where the "how do we go live" story is written, and the answer to "point it at
    production?" has to be *no* in the same document that explains the switch.
    """
    from seed.population import run_population
    from seed.targets import UnsafeTarget

    with pytest.raises(UnsafeTarget) as caught:
        run_population(
            pop_manifest,
            int(pop_manifest["seed"]),
            buyer_url="https://buyer.proxyshop.example",
            trust_url="http://127.0.0.1:9",
            client=object(),
        )
    assert "--allow-remote" in str(caught.value)


def test_the_dimension_rounding_grid_sits_above_the_decay_noise(seedfx_artifact: Any) -> None:
    """A transcript may not round a decayed quantity finer than the decay moves it.

    The defect this pins was live and invisible: ``posture_view`` rounded a dimension's
    ``alpha`` and ``beta`` at six decimal places, and with the approved 30-day half-life a run
    lasting a fraction of a second already moves a ``beta`` of 11 by more than that grid's
    5e-7 half-step. Measured before the repair — six full runs of one seed, three distinct
    transcript digests, each differing by exactly one unit in the sixth place of a single
    parameter while every score matched. "Byte-identical across two processes" was a claim the
    format could not keep, and the suite's own determinism test missed it because two episodes
    keep the parameters too small to reach a boundary.

    So this asserts the invariant rather than the constant: whatever the precision is, the
    grid has to be wider than the drift a real run of this corpus actually shows. It is
    computed from the artifact's own span and the manifest's own half-life, so it re-derives
    on any machine rather than encoding one machine's timings.
    """
    import math
    from datetime import datetime

    from seed.population import DIMENSION_PRECISION, POSTURE_PRECISION

    def _moment(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    span_days = (
        _moment(seedfx_artifact.as_of)
        - min(_moment(str(event["ts"])) for event in seedfx_artifact.events)
    ).total_seconds() / 86400.0
    assert span_days > 0, "the chain and the snapshot share an instant; nothing decayed"

    from fixtures.manifest import load_manifest

    half_life = float(load_manifest()["half_life_days"])
    largest = max(
        float(state[name])
        for entry in seedfx_artifact.closing_postures.values()
        for state in entry["dims"].values()
        for name in ("alpha", "beta")
    )
    drift = largest * (1.0 - math.exp(-math.log(2.0) / half_life * span_days))

    half_grid = 0.5 * 10.0**-DIMENSION_PRECISION
    assert half_grid > drift * 10.0, (
        f"a dimension parameter of {largest} drifts {drift:.3e} over this run's {span_days:.2e} "
        f"days, and the transcript rounds it on a grid whose half-step is {half_grid:.3e}. That "
        "is not enough margin: the digest will flip between runs and the byte-identity claim is "
        "false."
    )
    if drift > 0.5 * 10.0**-POSTURE_PRECISION:
        assert DIMENSION_PRECISION < POSTURE_PRECISION, (
            "on this machine the decay drift already exceeds the score grid's half-step, so the "
            "dimension parameters MUST be rounded coarser than the score. Collapsing the two "
            "back into one constant reintroduces the defect."
        )
