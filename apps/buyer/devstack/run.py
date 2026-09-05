#!/usr/bin/env python3
# ======================================================================================
# THE ONLY THING THIS DEMO SUPPLIES IS `demo-market.json`.
#
# That file holds merchant data and nothing else: three stores' approved envelopes, their
# catalogues, their live stock, the platform's trust reading of each of them, and the two
# conversations a reader can type in. It contains no shortlist, no ranking, no score, no
# price the exchange did not compute, no verdict and no permalink.
#
# Everything else on the screen is produced, this run, by real services talking to each
# other over real loopback sockets:
#
#   * three real `store_agent` apps, one per store, each bound to its own port, each
#     answering `POST /v1/bid-requests` out of its own envelope;
#   * one real `exchange` app, which solicits those three over HTTP, verifies their claims
#     against the catalogue snapshots, applies R12's blacklist, ranks what survives and
#     mints the checkout permalink;
#   * one real `buyer_svc` app, which clarifies the buyer's turns, opens the auction,
#     renders the shortlist and accepts a slot - and serves the built UI from the same
#     origin, so the browser talks to the same process a script would.
#
# Nothing here fakes a response, and nothing here shortcuts a service. If a store declines,
# the UI shows the decline. If the shortlist is empty, the UI shows the exchange's real
# exclusion reasons. That is the whole point of the exercise.
# ======================================================================================
"""The one-command dev stack: three store agents + the exchange + the buyer app.

Run it from the repo root::

    PROXYSHOP_WORKER=10 .venv/bin/python apps/buyer/devstack/run.py

or, the way a person is meant to::

    npm run demo --workspace @proxyshop/buyer

(``npm`` runs a workspace script with cwd = ``apps/buyer``, so the package's own scripts
spell the path as ``devstack/run.py``.)

This is a development tool. It is not shipped product, nothing imports it, and it lives
outside every package's import namespace on purpose.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# sys.path, before anything else is imported
# --------------------------------------------------------------------------------------
# This script is executed BY PATH (`.venv/bin/python devstack/run.py`), not as a module of
# an installed package, so nothing has put the repo on the path for it. Two entries are
# needed and they are different things: the repo root carries `proxyshop_support`, and
# `.pkgroot` carries the tracked symlinks that give every app its flat import namespace
# (`store_agent`, `exchange`, `buyer_svc`, `contracts`, ...). Missing either one turns a
# working stack into an ImportError with a misleading name.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
PKGROOT = REPO_ROOT / ".pkgroot"

for _entry in (REPO_ROOT, PKGROOT):
    _text = str(_entry)
    if _text not in sys.path:
        sys.path.insert(0, _text)

MARKET_FILE = HERE / "demo-market.json"
UI_DIST = REPO_ROOT / "apps" / "buyer" / "dist"

#: The buyer app's port. FIXED, unlike every other port here, because a person needs a URL
#: they can bookmark and reload. The store agents and the exchange bind port 0 (D40) - only
#: this process ever needs to know where they are.
DEFAULT_BUYER_PORT = 8100
BUYER_PORT_ENV = "BUYER_UI_PORT"

BUILD_HINT = "npm run demo --workspace @proxyshop/buyer"


class DevstackError(RuntimeError):
    """The stack could not be brought up. Always names what and why."""


# --------------------------------------------------------------------------------------
# the market file -> the shapes each service reads
# --------------------------------------------------------------------------------------
def load_market(path: Path = MARKET_FILE) -> dict[str, Any]:
    """The checked-in demo market, or a DevstackError naming the file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DevstackError(f"cannot read the demo market {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise DevstackError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DevstackError(f"{path} must hold a JSON object, got {type(document).__name__}")
    stores = document.get("stores")
    if not isinstance(stores, list) or not stores:
        raise DevstackError(f"{path} states no 'stores'; there is no market to run")
    return document


def store_context(store: dict[str, Any]) -> dict[str, Any]:
    """The store context ONE agent app advocates for.

    Built by name rather than by copying the market row, so the row's own bookkeeping keys
    (``_comment``, ``eligibility``, ``trust``, ``tier``) never reach the agent: eligibility
    and trust are the PLATFORM's readings of this store and belong to the exchange's
    deployment document, not to the merchant's own context.

    ``store_domain`` is carried here deliberately. ``store_agent.runtime`` builds the
    offer's ``checkout_url`` from it, the exchange excludes every candidate whose offer has
    none, and ``solicitation.serving`` documents that a domain stated in the context file
    outranks the ``STORE_AGENT_STORE_DOMAIN`` env var - which is exactly why this launcher
    can run three stores in one process at all.
    """
    return {
        "store_id": store["store_id"],
        "store_domain": store["store_domain"],
        "envelope": store["envelope"],
        "catalog": store["catalog"],
        "live_state": store["live_state"],
        "learned_policy": store.get("learned_policy"),
        "network_priors": store.get("network_priors", {}),
    }


def catalog_snapshot(store: dict[str, Any]) -> dict[str, Any]:
    """The catalogue snapshot the exchange grades this store's claims against.

    Shape measured from ``e2e/support/s1/flow.py:155``. Built from the same catalogue rows
    the agent reads, which is what makes a false claim detectable: the agent asserts what
    its catalogue says and the verifier reads that catalogue independently, so a divergence
    is a contradiction rather than a difference of opinion.
    """
    product_ref = str(store["product_ref"])
    catalog_row = dict(store["catalog"][product_ref])
    live_row = dict(store["live_state"].get(product_ref, {}))
    observed_at = "2026-01-01T00:00:00Z"

    attributes: dict[str, Any] = {
        key: {"value": value} for key, value in catalog_row.items() if key != "product_ref"
    }
    attributes.update({key: {"value": value} for key, value in live_row.items()})
    for commitment in store["envelope"].get("standing_commitments", ()):
        attributes[str(commitment["key"])] = {"value": commitment["value"]}

    return {
        "snapshot_id": f"snap-{store['store_id']}",
        "captured_at": observed_at,
        "store_id": store["store_id"],
        "products": [
            {
                "product_ref": product_ref,
                "canonical_name": product_ref,
                "evidence_ref": f"snap-{store['store_id']}#{product_ref}",
                "observed_at": observed_at,
                "attributes": attributes,
                "offer": {
                    "unit_price": float(catalog_row["list_price"]),
                    "currency": "USD",
                    "availability": "in_stock" if live_row.get("in_stock") else "out_of_stock",
                },
            }
        ],
    }


def exchange_deployment(market: dict[str, Any], agent_urls: dict[str, str]) -> dict[str, Any]:
    """The exchange's deployment document, pointing at the agents just bound.

    ``registered_domain`` is a BARE HOST on purpose - ``exchange.composition`` refuses a
    scheme or a port here, because the value is compared against a checkout URL's host and
    a mismatched one excludes every candidate for the store ``off_domain_checkout`` with no
    hint that the registry is the reason.
    """
    sellers = []
    trust_snapshot: dict[str, Any] = {}
    for store in market["stores"]:
        store_id = str(store["store_id"])
        sellers.append(
            {
                "store_id": store_id,
                "eligibility": str(store["eligibility"]),
                "registered_domain": str(store["store_domain"]),
                "bid_endpoint": f"{agent_urls[store_id]}/v1/bid-requests",
            }
        )
        trust = dict(store["trust"])
        trust_snapshot[store_id] = {
            "store_id": store_id,
            "blacklisted": bool(trust["blacklisted"]),
            "score": float(trust["score"]),
        }
    return {
        "sellers": sellers,
        "trust_snapshot": trust_snapshot,
        "checkout_mode": str(market.get("checkout_mode") or "redirect"),
    }


def buyer_roster(market: dict[str, Any]) -> list[dict[str, Any]]:
    """The candidate set the buyer service opens every auction with.

    Deployment data, never the browser's word: ``ExchangeHttpClient.create_auction`` fills
    it in only when the caller's payload carries none, so the page cannot nominate which
    stores compete for it.
    """
    roster = []
    for store in market["stores"]:
        product_ref = str(store["product_ref"])
        roster.append(
            {
                "store_id": str(store["store_id"]),
                "tier": int(store.get("tier", 1)),
                "product_ref": product_ref,
                "list_price": float(store["catalog"][product_ref]["list_price"]),
                "max_discount_pct": float(store["envelope"]["max_discount_pct"]),
            }
        )
    return roster


def buyer_deployment(market: dict[str, Any], exchange_url: str) -> dict[str, Any]:
    return {
        "exchange_base_url": exchange_url,
        "roster": buyer_roster(market),
        "request_timeout_seconds": 30.0,
    }


def write_document(directory: Path, name: str, document: Any) -> Path:
    path = directory / name
    path.write_text(json.dumps(document, indent=2, sort_keys=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# serving the buyer app on a FIXED port
# --------------------------------------------------------------------------------------
@contextlib.contextmanager
def serve_on_port(app: Any, port: int, *, host: str = "127.0.0.1", timeout: float = 20.0):
    """Run ``app`` on a fixed port in a background thread; yield its base URL.

    ``proxyshop_support.asgi_server.serve`` binds port 0 (D40) and is what the store agents
    and the exchange use - nothing outside this process needs to find them. The buyer app is
    the one service a PERSON has to reach, so it gets a stable URL instead.

    A port already in use is reported as such rather than as a start-up timeout: uvicorn
    logs its own bind error and exits the thread, so "the thread is gone and no port was
    bound" is a different condition from "the server is still coming up", and telling them
    apart is the difference between a useful message and twenty seconds of silence.
    """
    import uvicorn  # noqa: PLC0415 - deferred so `--help` needs no server stack

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="proxyshop-buyer-ui", daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            break
        if not thread.is_alive():
            raise DevstackError(
                f"the buyer app could not bind {host}:{port} - see uvicorn's error above. "
                f"Something else is probably already listening there; set {BUYER_PORT_ENV} "
                f"to a free port and run it again."
            )
        time.sleep(0.02)
    else:
        server.should_exit = True
        raise DevstackError(f"the buyer app did not bind {host}:{port} within {timeout}s")

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


# --------------------------------------------------------------------------------------
# the banner
# --------------------------------------------------------------------------------------
RULE = "=" * 86


def print_banner(
    *,
    market: dict[str, Any],
    agent_urls: dict[str, str],
    exchange_url: str,
    buyer_url: str,
    ui_dist: Path | None,
) -> None:
    out = sys.stdout
    print(f"\n{RULE}", file=out)
    print("  proxyshop devstack - the real backend, three real stores, one buyer app", file=out)
    print(RULE, file=out)

    print("\n  STORE AGENTS (each is one store's advocate; port 0, D40)", file=out)
    for store in market["stores"]:
        store_id = str(store["store_id"])
        trust = store["trust"]
        flag = "BLACKLISTED" if trust["blacklisted"] else f"trust {float(trust['score']):.2f}"
        print(
            f"    {store_id:<20} {agent_urls[store_id]}/v1/bid-requests"
            f"   [{store['store_domain']}, {flag}]",
            file=out,
        )

    print(f"\n  EXCHANGE   {exchange_url}", file=out)
    print(f"             {exchange_url}/docs", file=out)
    print(f"\n  BUYER APP  {buyer_url}      <- open this", file=out)
    print(f"             {buyer_url}/docs", file=out)

    if ui_dist is None:
        print(f"\n{RULE}", file=out)
        print("  !!  THE UI IS NOT BUILT.", file=out)
        print(f"  !!  Nothing exists at {UI_DIST}, so the buyer service mounts no static", file=out)
        print("  !!  files and the URL above answers 404 at '/'. The API is fully up and", file=out)
        print("  !!  every route below /buyer works; only the page is missing.", file=out)
        print(f"  !!  Build it and start again with:   {BUILD_HINT}", file=out)
        print(RULE, file=out)
    else:
        print(f"             serving the built UI from {ui_dist}", file=out)

    print("\n  DEMO CONVERSATIONS - type the turns in order, exactly as written.", file=out)
    print("  The wording is load-bearing: cluster_id is a hash over the clarified", file=out)
    print("  query, band and constraints, and a store only bids for a cluster its", file=out)
    print("  envelope pursues.", file=out)
    print("  The clarifier keeps asking until it has asked its three (R1's cap), so", file=out)
    print("  after the turns below you will still be asked one or two more. Answer", file=out)
    print("  them with anything that adds no new constraint ('no') - the cluster is", file=out)
    print("  already fixed by the turns below, and the Confirm button appears once", file=out)
    print("  nothing is outstanding.\n", file=out)
    for conversation in market.get("conversations", ()):
        print(f"    [{conversation['id']}] {conversation.get('title', '')}", file=out)
        for index, turn in enumerate(conversation.get("turns", ()), start=1):
            print(f"        {index}. {turn}", file=out)
        print(f"        -> cluster {conversation['cluster_id']}", file=out)
        for line in conversation.get("reaches", ()):
            print(f"           {line}", file=out)
        print("", file=out)

    print(f"{RULE}", file=out)
    print("  Ctrl-C to shut everything down.", file=out)
    print(f"{RULE}\n", file=out)
    out.flush()


# --------------------------------------------------------------------------------------
# the stack
# --------------------------------------------------------------------------------------
def run(*, open_browser: bool, port: int) -> int:
    from proxyshop_support.asgi_server import serve  # noqa: PLC0415

    market = load_market()

    with contextlib.ExitStack() as stack:
        workdir = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="proxyshop-devstack-"))
        )

        # -- 1. one store agent app per store, all in this one process ------------------
        # `configure_solicitation(app, context=...)`, NOT `STORE_AGENT_CONTEXT`. That env
        # var is process-wide and names exactly one file, so it cannot describe three
        # stores; per-app wiring is the seam `store_agent.solicitation.serving` publishes
        # for a composition root that already holds the contexts in memory.
        import store_agent.main  # noqa: PLC0415
        from store_agent.solicitation.serving import configure_solicitation  # noqa: PLC0415

        agent_urls: dict[str, str] = {}
        for store in market["stores"]:
            agent_app = store_agent.main.create_app()
            configure_solicitation(agent_app, context=store_context(store))
            agent_urls[str(store["store_id"])] = stack.enter_context(serve(agent_app))

        # -- 2. the exchange -----------------------------------------------------------
        deployment_path = write_document(
            workdir, "exchange-deployment.json", exchange_deployment(market, agent_urls)
        )
        os.environ["EXCHANGE_DEPLOYMENT"] = str(deployment_path)

        import exchange.main  # noqa: PLC0415
        from exchange.ranking.serving import configure_ranking  # noqa: PLC0415

        exchange_app = exchange.main.create_app()

        # ==============================================================================
        # THIS CALL IS REACHING PAST A DEFECT IN THE EXCHANGE'S COMPOSITION ROOT.
        #
        # `apps/exchange/src/composition.py` contains the string "catalog" ZERO times, so a
        # deployment document has no way to express a catalog snapshot and `configure_
        # exchange` binds none. `exchange.ranking.serving.catalog_of` then falls back to
        # `NoCatalogSnapshots`, which holds a snapshot for nobody, so `claim_verification.
        # verify` is never run, no claim comes back verified, and R19 refuses to let an
        # unverified claim satisfy a hard constraint.
        #
        # Measured consequence with this exact stack and this call removed: an intent
        # carrying ANY hard constraint gets `ranked: []` and every candidate lands in
        # `excluded[]` with `hard_constraint_unsatisfied`. Both demo conversations carry
        # hard constraints, so without this line the demo shows an empty shortlist for
        # every question - and it would look like a policy decision rather than a wiring
        # hole.
        #
        # So this is NOT a demo convenience or a shortcut around a real check. It is a
        # deployment supplying a collaborator that its own deployment-document format
        # cannot name. The right fix is a `catalog` section in the exchange's deployment
        # document and a `configure_ranking(app, catalog=...)` inside `configure_exchange`;
        # until that exists, every deployment of this exchange has to make this call by
        # hand, exactly as this launcher does.
        # ==============================================================================
        configure_ranking(
            exchange_app,
            catalog={str(store["store_id"]): catalog_snapshot(store) for store in market["stores"]},
        )

        exchange_url = stack.enter_context(serve(exchange_app))

        # -- 3. the buyer service, and the UI it serves ---------------------------------
        buyer_path = write_document(
            workdir, "buyer-deployment.json", buyer_deployment(market, exchange_url)
        )
        os.environ["BUYER_DEPLOYMENT"] = str(buyer_path)

        # Set only when the directory really exists. `buyer_svc.composition` mounts static
        # files when the variable names a real directory and mounts nothing when it does
        # not; pointing it at an absent path would be asking it to decide what an operator
        # meant, and "the UI is not built" is a fact this launcher can check itself.
        ui_dist: Path | None = UI_DIST if UI_DIST.is_dir() else None
        if ui_dist is not None:
            os.environ["BUYER_UI_DIST"] = str(ui_dist)
        else:
            os.environ.pop("BUYER_UI_DIST", None)

        try:
            import buyer_svc.composition  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            raise DevstackError(
                "buyer_svc.composition could not be imported "
                f"({exc}). The buyer composition root lives at "
                "apps/buyer/svc/src/composition.py (reached through .pkgroot/buyer_svc) and "
                "is what wires the buyer service to the exchange. Without it the stack has "
                "nothing to serve the journey from."
            ) from exc

        buyer_app = buyer_svc.composition.create_app()
        buyer_url = stack.enter_context(serve_on_port(buyer_app, port))

        # -- 4. tell the person what is running ----------------------------------------
        print_banner(
            market=market,
            agent_urls=agent_urls,
            exchange_url=exchange_url,
            buyer_url=buyer_url,
            ui_dist=ui_dist,
        )

        if open_browser and ui_dist is None:
            print(
                f"  Not opening a browser: there is no built UI to show. Run `{BUILD_HINT}`.\n",
                file=sys.stdout,
            )
        elif open_browser:
            webbrowser.open(buyer_url)

        stop = threading.Event()
        restore = _install_stop_handlers(stop)
        try:
            while not stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            # Only reachable when no handler could be installed (`signal.signal` raises off
            # the main thread). Kept so the fallback path still shuts down rather than
            # printing a traceback.
            pass
        print("\n  shutting down...", file=sys.stdout)

    restore()
    print("  stopped.", file=sys.stdout)
    return 0


def _install_stop_handlers(stop: threading.Event):
    """Ask for a clean shutdown on SIGINT/SIGTERM, and IGNORE every later one.

    One Ctrl-C reaches this process more than once, which is the whole reason this exists
    rather than a bare ``except KeyboardInterrupt``. A terminal delivers SIGINT to the entire
    foreground process GROUP, and ``npm run`` forwards it to its child as well — so under
    ``npm run demo`` the second copy landed while the ExitStack was already tearing the
    servers down, interrupted ``thread.join`` inside a ``finally``, and printed a
    KeyboardInterrupt traceback over an otherwise clean shutdown (measured; npm reported
    exit status -2). Re-arming the signal as ``SIG_IGN`` inside the handler makes the first
    one the only one that is acted on, so teardown always runs to completion.

    Returns a callable that puts the previous handlers back.
    """
    import signal  # noqa: PLC0415

    previous: dict[int, Any] = {}

    def requested_stop(signum: int, _frame: Any) -> None:
        signal.signal(signum, signal.SIG_IGN)
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, requested_stop)
        except (ValueError, OSError):
            # Not the main thread, or a platform that will not take this signal. The
            # KeyboardInterrupt fallback above covers it.
            continue

    def restore() -> None:
        for signum, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)

    return restore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="devstack/run.py",
        description=(
            "Boot the real proxyshop backend for the buyer UI demo: one store agent per "
            "store in demo-market.json, the exchange, and the buyer app - all in this "
            "process, all talking over real loopback sockets."
        ),
        epilog=(
            "The only data this demo supplies is devstack/demo-market.json (merchant "
            "envelopes, catalogues, stock, trust readings and the demo conversations). "
            "Every price, score, exclusion and permalink you see is computed by the real "
            "services at run time."
        ),
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="do not open a browser at the buyer app's URL",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"port for the buyer app (default: ${BUYER_PORT_ENV}, else {DEFAULT_BUYER_PORT}). "
            f"The store agents and the exchange always bind port 0."
        ),
    )
    return parser.parse_args(argv)


def resolve_port(explicit: int | None) -> int:
    if explicit is not None:
        return int(explicit)
    raw = str(os.environ.get(BUYER_PORT_ENV) or "").strip()
    if not raw:
        return DEFAULT_BUYER_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise DevstackError(
            f"{BUYER_PORT_ENV}={raw!r} is not a port number; unset it or write an integer"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(open_browser=not args.no_open, port=resolve_port(args.port))
    except DevstackError as failure:
        print(f"\ndevstack: {failure}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
