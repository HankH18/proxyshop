"""``python -m sim`` — run the whole network headless, in bounded steps.

T-081 acceptance 3 is "the sim runs headless against compose in bounded steps", and this is
the entry point that does it. It takes no services, opens no sockets and touches no
database: the run is a pure function of the human-approved manifest and one seed, so it is
the same run whether the compose stack is up or down. That is deliberate — a demo step that
only works when three datastores happen to be reachable is a demo step that fails in front
of an audience.

Exit status is the finding, not decoration:

``0``
    the run completed and the trust engine caught the manifest's dishonest store inside the
    approved episode budget.
``1``
    the run completed and the dishonest store was **never** caught. This is the S2 failure
    and it must not look like success.
``2``
    the run could not start — usually a manifest whose digest chain no longer matches.

``--json`` writes the canonical run to stdout, which is what makes acceptance 1 checkable
from a shell: two runs of the same seed produce byte-identical output.
"""

from __future__ import annotations

__all__ = ["build_parser", "main"]

import argparse
import json
import sys
from collections.abc import Sequence

from .dishonest import behaviour_kinds
from .runner import run_simulation


def build_parser() -> argparse.ArgumentParser:
    """The command line. ``--seed`` defaults to the manifest's own approved seed."""
    parser = argparse.ArgumentParser(
        prog="python -m sim",
        description="Run the seeded ProxyShop market simulation headless.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="the run seed; defaults to the approved manifest's `seed`",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        default=None,
        help=(
            "shorten the run to this many episodes. It cannot be lengthened past the "
            "manifest's approved episode_budget."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write the canonical run to stdout instead of the human summary",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the simulation and report. See the module docstring for the exit statuses."""
    from fixtures.manifest import ManifestError, load_manifest

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        manifest = load_manifest()
    except ManifestError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2

    seed = args.seed if args.seed is not None else int(manifest["seed"])
    run = run_simulation(manifest, seed, episodes=args.episodes)

    if args.json:
        json.dump(run.to_json(), sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
    else:
        _summarise(run, behaviour_kinds(manifest))
    return 0 if run.caught_at is not None else 1


def _summarise(run: object, scripted: Sequence[str]) -> None:
    """The human-readable report. Names what did NOT happen as loudly as what did.

    The approved behaviour list is printed first, and from the manifest rather than from the
    run: a reader comparing this output to `fixtures/manifest.json` should be able to see
    that the attack the simulator replayed is the attack a human signed, without trusting
    the simulator's own account of it.
    """
    episodes = getattr(run, "episodes", ())
    print(f"approved dishonest script ({len(scripted)} behaviours): {', '.join(scripted)}")
    print(f"seed={getattr(run, 'seed', '?')} category={getattr(run, 'seed_category', '?')}")
    print(f"episodes={len(episodes)} chain_ok={getattr(run, 'chain_ok', False)}")
    for episode in episodes:
        print(
            f"  ep{episode.episode:>3} winner={episode.winner or '-':<20}"
            f" accepted={str(episode.accepted):<5} denied={len(episode.denied)}"
            f" score={episode.score:.4f}"
        )
    caught = getattr(run, "caught_at", None)
    if caught is None:
        print(
            "S2 NOT SATISFIED: the dishonest store was never scored below the blacklist "
            "threshold inside the approved episode budget."
        )
    else:
        print(f"S2: the dishonest store fell below the blacklist threshold at episode {caught}.")

    problems = getattr(run, "ledger_contract_problems", ())
    if problems:
        print(f"ledger payload contract deviations ({len(problems)}):")
        for kind, problem in problems:
            print(f"  {kind}: {problem}")


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
