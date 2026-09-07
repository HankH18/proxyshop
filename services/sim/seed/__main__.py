"""``python -m seed`` — the seeding script, and the two things you do with what it made.

Three subcommands, and the split between them is the owner's ruling made executable:

``run``
    Stand the population up **once**, drive the real feedback routes with it, and store what
    it produced (:mod:`seed.store`). This is the part that is a script rather than a service:
    a person runs it deliberately, it exits, and the artifact is what survives.
``replay``
    Reproduce every posture from the stored artifact, offline, with nothing simulated. This
    is what a demo runs. It touches no service, opens no socket, mints nothing, and returns
    the same bytes every time because it decays against the ``as_of`` the artifact recorded
    rather than against the clock.
``posture``
    The audit. Every store's posture computed twice — with the manufactured observations and
    without them — off the stored chain. See :mod:`seed.provenance` for why that has to be
    possible before the first real customer arrives rather than after.

Exit status is the finding, not decoration
------------------------------------------
``run`` exits **0** only when the manifest's over-promising store ends the run below the
manifest's honest control. A population that answered every prompt and left the two
indistinguishable has demonstrated that R14's channel does nothing, and that must not look
like success (:func:`seed.population._separated` owns the rule). ``replay`` exits **0** only
when the postures it recomputes from the stored chain match the ones the served routes
produced during the run; a mismatch means the artifact's chain and its transcript disagree,
which is exactly the corruption a stored corpus must not be able to hide. **2** from any
subcommand means it could not start.

Where it may point
------------------
Loopback, unless the operator says ``--allow-remote`` on the command line. This tool
manufactures trust signal onto an append-only ledger with no delete; :mod:`seed.targets`
holds the guard and the argument for it.
"""

from __future__ import annotations

__all__ = ["build_parser", "main"]

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from typing import Any

from .population import (
    DEFAULT_SHOPPERS_PER_EPISODE,
    PopulationError,
    _separated,
    _summarise,
    run_population,
    transcript,
)
from .provenance import posture_split
from .shoppers import (
    DEFAULT_NOISE_RATE,
    DEFAULT_RESPONSE_RATE,
    ShopperPolicy,
    ShopperPolicyError,
)
from .store import SeedArtifactError, default_root, load, replay_postures, write

#: How many events one ``GET /events`` page may return. Only the ``--trust-url`` path uses
#: it; a private local stack hands its chain over in process.
_EVENT_PAGE = 1000


def build_parser() -> argparse.ArgumentParser:
    """The command line. Defaults to a private stack on loopback and refuses anything else."""
    parser = argparse.ArgumentParser(
        prog="python -m seed",
        description=(
            "Seed the R14 buyer-feedback channel with a population of simulated returning "
            "shoppers, store what it produced, and replay it. EVERY answer it writes is "
            "SIMULATED and is labelled as such on the ledger, permanently."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the population once and store what it produced")
    run.add_argument("--seed", type=int, default=None, help="defaults to the manifest's seed")
    run.add_argument("--episodes", type=int, default=None, help="shorten the run")
    run.add_argument(
        "--shoppers",
        type=int,
        default=DEFAULT_SHOPPERS_PER_EPISODE,
        help=f"shoppers per simulated day (default {DEFAULT_SHOPPERS_PER_EPISODE})",
    )
    run.add_argument("--response-rate", type=float, default=None, help="0..1")
    run.add_argument("--noise-rate", type=float, default=None, help="0..1")
    run.add_argument(
        "--buyer-url",
        default=None,
        help="an already-running buyer service. Omit BOTH urls to run a private local stack.",
    )
    run.add_argument("--trust-url", default=None, help="an already-running trust service")
    run.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "permit a non-loopback service. This tool writes synthetic buyer feedback onto an "
            "append-only ledger that moves a real store's published trust posture."
        ),
    )
    run.add_argument(
        "--out",
        default=None,
        help=f"where to store the artifact (default {default_root()})",
    )
    run.add_argument(
        "--no-store",
        action="store_true",
        help="run and report without writing an artifact",
    )
    run.add_argument("--json", action="store_true", help="write the canonical transcript")

    replay = sub.add_parser(
        "replay",
        help="reproduce the stored postures offline, without re-simulating anything",
    )
    replay.add_argument("--from", dest="source", default=None, help="the artifact directory")
    replay.add_argument("--json", action="store_true", help="write the replayed postures")

    posture = sub.add_parser(
        "posture",
        help="every store's posture with the seeded observations, and without them",
    )
    posture.add_argument("--from", dest="source", default=None, help="the artifact directory")
    posture.add_argument("--store", default=None, help="report one store rather than all of them")
    posture.add_argument("--json", action="store_true", help="write the split as JSON")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to a subcommand. See this module's docstring for what each status means."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "run":
        return _run(args)
    if args.command == "replay":
        return _replay(args)
    return _posture(args)


# =====================================================================================
# run — the part that is a script
# =====================================================================================
def _run(args: argparse.Namespace) -> int:
    import httpx

    from fixtures.manifest import ManifestError, load_manifest

    from .targets import UnsafeTarget

    try:
        manifest = load_manifest()
    except ManifestError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2

    if (args.buyer_url is None) != (args.trust_url is None):
        print(
            "give BOTH --buyer-url and --trust-url, or neither. One address and one guess is "
            "how a population ends up writing feedback into a service nobody chose.",
            file=sys.stderr,
        )
        return 2

    seed = args.seed if args.seed is not None else int(manifest["seed"])
    try:
        policy = ShopperPolicy(
            response_rate=(
                DEFAULT_RESPONSE_RATE if args.response_rate is None else args.response_rate
            ),
            noise_rate=DEFAULT_NOISE_RATE if args.noise_rate is None else args.noise_rate,
        )
    except ShopperPolicyError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2

    with ExitStack() as stack:
        from sim.runner import build_roster

        from fixtures.generator import generate

        roster = build_roster(
            generate(str(manifest["seed_category"]), int(manifest["seed"])), manifest
        )
        chain_source: Any = None
        if args.buyer_url is None:
            from .local_stack import local_stack

            services = stack.enter_context(local_stack(roster))
            buyer_url, trust_url = services.buyer_url, services.trust_url
            chain_source = services.event_store
        else:
            buyer_url, trust_url = str(args.buyer_url), str(args.trust_url)

        client = stack.enter_context(httpx.Client(timeout=30.0))
        try:
            run = run_population(
                manifest,
                seed,
                buyer_url=buyer_url,
                trust_url=trust_url,
                client=client,
                policy=policy,
                episodes=args.episodes,
                shoppers_per_episode=args.shoppers,
                allow_remote=bool(args.allow_remote),
            )
        except (UnsafeTarget, PopulationError, ShopperPolicyError) as exc:
            print(f"cannot run: {exc}", file=sys.stderr)
            return 2

        if not args.no_store:
            try:
                events = (
                    list(chain_source.read())
                    if chain_source is not None
                    else _chain_over_http(client, trust_url)
                )
                record = write(
                    args.out or default_root(),
                    run=run,
                    events=events,
                    roster=roster,
                    manifest=manifest,
                    producer=_producer(args, seed, len(run.postures)),
                    episodes=len(run.postures),
                )
            except (SeedArtifactError, PopulationError) as exc:
                print(f"the run completed and the artifact was NOT stored: {exc}", file=sys.stderr)
                return 2
            if not args.json:
                _report_stored(args.out or default_root(), record)

        if args.json:
            json.dump(transcript(run), sys.stdout, sort_keys=True, separators=(",", ":"))
            sys.stdout.write("\n")
            return 0 if _separated(run, manifest) else 1
        _summarise(run, manifest)
        return 0 if _separated(run, manifest) else 1


def _producer(args: argparse.Namespace, seed: int, episodes: int) -> str:
    """The exact command that reproduces this artifact, written into its provenance.

    Spelled out with every value resolved rather than echoing what was typed: "the default"
    is not a reproducible instruction once the default moves, and the whole purpose of the
    field is that somebody who has only the artifact can run the line and compare digests.
    """
    parts = [
        "python -m seed run",
        f"--seed {seed}",
        f"--episodes {episodes}",
        f"--shoppers {args.shoppers}",
    ]
    if args.response_rate is not None:
        parts.append(f"--response-rate {args.response_rate}")
    if args.noise_rate is not None:
        parts.append(f"--noise-rate {args.noise_rate}")
    return " ".join(parts)


def _chain_over_http(client: Any, trust_url: str) -> list[dict[str, Any]]:
    """The whole trust chain, read back through ``GET /events``, page by page.

    Paged rather than asked for in one gulp because the route caps a page and reports
    ``truncated`` when it cut one; a caller that ignored that would store a *prefix* of the
    chain under a provenance record claiming it was the chain, which is the specific dishonesty
    this module must not commit. A page that cannot be read is a refusal to store, never a
    short artifact.
    """
    events: list[dict[str, Any]] = []
    after_seq = 0
    while True:
        response = client.get(
            f"{trust_url.rstrip('/')}/events",
            params={"after_seq": after_seq, "limit": _EVENT_PAGE},
        )
        if response.status_code != 200:
            raise SeedArtifactError(
                f"GET /events answered {response.status_code}, so the chain this run wrote "
                f"could not be read back and there is nothing honest to store. "
                f"Body: {response.text[:200]}"
            )
        body = response.json()
        page = body.get("events") if isinstance(body, Mapping) else None
        if not isinstance(page, list):
            raise SeedArtifactError("GET /events returned no `events` list")
        events.extend(dict(event) for event in page)
        if not body.get("truncated"):
            return events
        after_seq = int(body.get("next_after_seq") or after_seq)


def _report_stored(root: Any, record: Mapping[str, Any]) -> None:
    marker = record.get("marker") or {}
    determinism = record.get("determinism") or {}
    print("=" * 78)
    print(f"STORED  {root}")
    print(f"  seeded events   {marker.get('seeded_events')}")
    print(f"  organic events  {marker.get('organic_events')}")
    print(f"  marker          {marker.get('field')} starts with {marker.get('prefix')!r}")
    print(f"  transcript      sha256:{determinism.get('transcript_digest')}")
    print(f"  as_of           {record.get('as_of')}")
    print("  replay it with  python -m seed replay")
    print("  audit it with   python -m seed posture")


# =====================================================================================
# replay — the part a demo runs
# =====================================================================================
def _replay(args: argparse.Namespace) -> int:
    from .population import posture_view

    try:
        artifact = load(args.source)
    except SeedArtifactError as exc:
        print(f"cannot replay: {exc}", file=sys.stderr)
        return 2

    replayed = posture_view(replay_postures(artifact))
    stored = artifact.closing_postures
    matched = replayed == stored

    if args.json:
        json.dump(replayed, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0 if matched else 1

    print("=" * 78)
    print("REPLAYED SIMULATED SEED DATA — nothing was re-simulated and no service was called.")
    print("=" * 78)
    print(f"artifact   {artifact.root}")
    print(f"seed       {artifact.seed}")
    print(f"as_of      {artifact.as_of}")
    print(f"events     {len(artifact.events)} on the stored chain")
    print()
    for store_id in sorted(replayed):
        entry = replayed[store_id]
        flag = " low_data" if entry.get("low_data") else ""
        print(f"  {store_id:24s} score {float(entry['score']):.6f}{flag}")
    print()
    if matched:
        print("MATCHES the postures the served routes returned during the run, exactly.")
        return 0
    print(
        "MISMATCH: the stored chain does not reproduce the stored postures. The artifact's\n"
        "ledger.jsonl and its transcript.json disagree, which means one of them is not from\n"
        "the run the other describes. Do not use this corpus.",
        file=sys.stderr,
    )
    for store_id in sorted(set(stored) | set(replayed)):
        if stored.get(store_id) != replayed.get(store_id):
            print(f"  {store_id}: stored={stored.get(store_id)} replayed={replayed.get(store_id)}")
    return 1


# =====================================================================================
# posture — the audit the marker exists for
# =====================================================================================
def _posture(args: argparse.Namespace) -> int:
    try:
        artifact = load(args.source)
    except SeedArtifactError as exc:
        print(f"cannot audit: {exc}", file=sys.stderr)
        return 2

    split = posture_split(artifact.events, artifact.roster, as_of=artifact.as_of)
    if args.store is not None:
        if args.store not in split:
            print(
                f"{args.store!r} is not a store in this artifact. It rosters: "
                f"{', '.join(sorted(split))}",
                file=sys.stderr,
            )
            return 2
        split = {args.store: split[args.store]}

    if args.json:
        json.dump(
            {store_id: row.to_json() for store_id, row in split.items()},
            sys.stdout,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stdout.write("\n")
        return 0

    print("=" * 78)
    print("POSTURE WITH AND WITHOUT THE SEEDED OBSERVATIONS")
    print("=" * 78)
    print(f"artifact  {artifact.root}")
    print(f"as_of     {artifact.as_of}")
    marker = artifact.collection.get("marker") or {}
    print(f"marker    {marker.get('field')} starts with {marker.get('prefix')!r}")
    print()
    print(f"  {'store':24s} {'with seed':>10s} {'earned':>10s} {'seeded':>8s} {'organic':>8s}")
    for store_id, row in sorted(split.items()):
        print(
            f"  {store_id:24s} {row.with_seed:10.6f} {row.without_seed:10.6f} "
            f"{row.seeded_observations:8d} {row.organic_observations:8d}"
        )
    print()
    print(
        "'earned' is what this store's posture would be if every manufactured observation had\n"
        "never been written. Both columns come from the same scorer over the same chain at the\n"
        "same instant; only the admitted events differ."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
