"""``python -m fixtures.seed`` — the command ``make demo-seed`` runs.

    make demo-seed SEED_CATEGORY=coffee

Reads the approved manifest for the seed and the store roster, generates the category's
catalog, and upserts the variants into the local shopify-stub. Run it twice: the second run
reports ``created: 0`` and every variant ``unchanged``.

``--dry-run`` does everything except the HTTP call, which is what a machine with no stub
running (or a `make verify` pipeline, C9) should use.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from fixtures.generator import CATALOG_DIR, GeneratorError
from fixtures.manifest import ManifestError
from fixtures.seed import DEFAULT_STUB_URL, SeedError, build_payload, seed_stub, variant_body


def _known_categories() -> list[str]:
    """The SEED_CATEGORY values that actually have a config, for the error message.

    A "no config for 'tea'" that does not say which categories DO exist sends the reader
    looking for a directory listing the tool could have printed.
    """
    if not CATALOG_DIR.is_dir():
        return []
    return sorted(p.stem for p in CATALOG_DIR.glob("*.json") if p.is_file())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fixtures.seed",
        description="Seed the generated SEED_CATEGORY catalog into a running shopify-stub.",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="SEED_CATEGORY to generate; defaults to the approved manifest's seed_category.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Override the manifest's seed. Changing it changes the generated catalog.",
    )
    parser.add_argument(
        "--stub-url",
        default=DEFAULT_STUB_URL,
        help=f"Base URL of the shopify-stub (default {DEFAULT_STUB_URL}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and report without contacting the stub. No live store is ever touched.",
    )
    return parser


def main(argv: Sequence[str] | None = None, client: object | None = None) -> int:
    """Entry point. ``client`` is injectable so a test drives the real path, not a mock."""
    args = _parser().parse_args(list(argv) if argv is not None else None)

    # An empty --category is what `make demo-seed` passes when SEED_CATEGORY is unset; fall
    # back to the manifest rather than failing on an empty string.
    category = (args.category or "").strip() or None

    # `make demo-seed SEED_CATEGORY=tea` reaches exactly this line, and building the payload
    # is the step most likely to refuse: an unknown category, an unreadable category config,
    # a manifest whose digest chain has drifted. All three used to escape as a raw traceback
    # from the module's ONLY production entry point — a `make` target printing a stack trace
    # reads as "the tool is broken", not as "you named a category that does not exist".
    try:
        payload = build_payload(category, args.seed)
        body = variant_body(payload)
    except GeneratorError as exc:
        known = _known_categories()
        print(
            f"FATAL: {exc}\n"
            f"       SEED_CATEGORY must name a category config in "
            f"{CATALOG_DIR.relative_to(CATALOG_DIR.parents[1]).as_posix()}/. "
            f"Available: {', '.join(known) if known else '(none)'}\n"
            "       Either run `make demo-seed SEED_CATEGORY=<one of those>`, or add the "
            "config and re-pin the manifest\n"
            "       (./.venv/bin/python -m fixtures.manifest --refresh-digests).",
            file=sys.stderr,
        )
        return 3
    except ManifestError as exc:
        print(
            f"FATAL: the approved fixture manifest is not usable ground truth: {exc}\n"
            "       Nothing was seeded. Inspect it with "
            "`./.venv/bin/python -m fixtures.manifest` (read-only).",
            file=sys.stderr,
        )
        return 3
    except SeedError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 3

    summary = {
        "seed_category": payload["seed_category"],
        "seed": payload["seed"],
        "stores": len(payload["stores"]),
        "products": len(payload["catalog"]),
        "variants": len(body["variants"]),
        "dishonest_store_id": payload["dishonest_store_id"],
    }

    if args.dry_run:
        print(json.dumps({"mode": "dry-run", **summary}, indent=2))
        return 0

    if client is None:
        import httpx

        client = httpx.Client(timeout=10.0)
        owned = True
    else:
        owned = False
    try:
        report = seed_stub(client, args.stub_url, payload)
    except SeedError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a connection problem must read as a diagnosis
        print(
            f"FATAL: could not reach the shopify-stub at {args.stub_url}: {exc}\n"
            "       Start it, point --stub-url / SHOPIFY_STUB_URL at it, or use --dry-run.",
            file=sys.stderr,
        )
        return 2
    finally:
        if owned:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    print(json.dumps({"mode": "stub", "stub_url": args.stub_url, **summary, **report}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
