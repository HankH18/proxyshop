#!/usr/bin/env bash
# Is the shopper demo actually serving? — an after-deploy probe, not a healthcheck.
#
# Every container in this stack can report `(healthy)` while the demo is dead, and each of
# those states has been measured on this repository at least once:
#
#   * the exchange's probe checks Postgres, Redis, Neo4j and its own HTTP port, and says
#     nothing about whether a deployment document was found -- an exchange with none answers
#     every auction `ranked: []`;
#   * a store agent's probe fetches `/openapi.json`, which answers 200 whether or not the
#     store context resolved, so a typo'd STORE_AGENT_CONTEXT is a healthy container that
#     declines every solicitation;
#   * an empty Neo4j is a perfectly healthy Neo4j, and the graph roster over one finds nobody.
#
# So this drives the money path instead of asking the containers how they feel: it opens a
# real auction over HTTP with NO roster in the request body, which is the request only the
# graph can answer, and it fails loudly on each of the ways that can come back empty.
set -euo pipefail

cd "$(dirname "$0")/.."

EXCHANGE="${EXCHANGE_PROBE_URL:-http://localhost:${EXCHANGE_PORT:-8083}}"
# The trust service is probed too, and not for its health. `deploy/demo/exchange-deployment.json`
# states no `trust_snapshot`, so the exchange's ranking gate reads `GET /snapshot` live and R12
# EXCLUDES every store that snapshot holds no row for. On a stack where `make demo-trust` has not
# been run that is all ten of them, and the only symptom at the auction is `shortlist: []` — a
# message that points at ranking and says nothing about trust. This probe reads the snapshot
# itself so the operator is told which step is missing rather than left to guess.
TRUST="${TRUST_PROBE_URL:-http://localhost:${TRUST_PORT:-8084}}"
QUERY="${DEMO_QUERY:-milk thistle silymarin liver support extract}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "FATAL: python3 is not on PATH." >&2
  exit 2
fi

echo "opening an auction on ${EXCHANGE}/auctions with NO roster in the body ..."
echo "  intent query: ${QUERY}"
echo

EXCHANGE="$EXCHANGE" TRUST="$TRUST" QUERY="$QUERY" python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

url = os.environ["EXCHANGE"].rstrip("/") + "/auctions"
# A COMPLETE `Intent` and `BuyerProfile`, not the minimum the exchange's own request model
# accepts. `CreateAuctionRequest.intent` is a bare `dict`, so a partial intent is a 201 here
# and then a 422 at every store agent's door -- the exchange forwards the caller's intent
# verbatim and the agent validates it against the published contract. Measured with a
# four-key intent and no profile: seven shops found, `entries` all `fallback` with
# `store_refused:422`, and nothing in the auction response saying which five fields were
# missing. `preferences`, `created_at` and `schema_version` are required on `Intent`;
# `pseudonym` and `buckets` are required on `BuyerProfile`.
body = {
    "intent": {
        "intent_id": "demo-probe-1",
        "cluster_id": "cluster-liver-support",
        "query": os.environ["QUERY"],
        "hard_constraints": [],
        "preferences": [],
        "currency": "USD",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": "1.0.0",
    },
    # Coarse buckets and a rotating pseudonym: R13 forbids an identity field here, and the
    # store agent sees only this.
    "profile": {
        "pseudonym": "demo-probe-shopper",
        "buckets": {"budget_band": "0-50", "first_time": True},
    },
    # No "roster" key at all. That is the whole point of this probe: the candidate set has to
    # come from the catalogue graph, not from the caller.
}
request = urllib.request.Request(
    url,
    data=json.dumps(body).encode("utf-8"),
    headers={"content-type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", "replace")[:600]
    print(f"FAIL: POST {url} answered HTTP {exc.code}\n{detail}", file=sys.stderr)
    raise SystemExit(1) from None
except OSError as exc:
    print(f"FAIL: cannot reach {url}: {exc}", file=sys.stderr)
    print("      Is the stack up? `docker compose --profile demo ps`", file=sys.stderr)
    raise SystemExit(1) from None

source = payload.get("roster_source") or {}
entries = payload.get("entries") or []
ranked = payload.get("ranked") or []
slots = ((payload.get("shortlist") or {}).get("slots")) or []

print(json.dumps(payload, indent=2)[:4000])
print()
print(f"HTTP {status}   auction {payload.get('auction_id')}   state {payload.get('state')}")
print(
    f"roster_source: source={source.get('source')!r} shops={source.get('shops')} "
    f"products_considered={source.get('products_considered')} "
    f"reason={source.get('reason')!r} elapsed_ms={source.get('elapsed_ms')}"
)
print(f"entries={len(entries)}  ranked={len(ranked)}  shortlist slots={len(slots)}")

problems = []
if source.get("source") != "neo4j":
    problems.append(
        f"roster_source.source is {source.get('source')!r}, not 'neo4j'. "
        "'unwired' means EXCHANGE_SHOP_ROSTER is not `graph` in the exchange container, or "
        "EXCHANGE_DEPLOYMENT names no document (the roster branch is only reached once a "
        "deployment document is read). 'request' means this probe sent a roster, which it "
        "does not."
    )
if not source.get("shops"):
    problems.append(
        f"the graph returned no shops (reason: {source.get('reason')!r}). An empty Neo4j is "
        "the usual cause -- run `make demo-corpus`."
    )
if not entries:
    problems.append(
        "no store was collected. Every rostered store failed the eligibility gate, which "
        "means the deployment document's `sellers` do not name the graph's store ids."
    )
if not slots:
    problems.append("the shortlist is empty: nothing survived ranking.")

# WHY the shortlist is empty, when the reason is trust. R12 fails closed on a store the live
# snapshot holds no row for, so an unseeded trust service excludes every candidate and the
# auction above reports only the symptom. `exclusion_reasons` names it per store, and this reads
# the snapshot directly so the answer does not depend on the exchange choosing to publish them.
try:
    with open("deploy/demo/exchange-deployment.json", encoding="utf-8") as handle:
        rostered = [str(row["store_id"]) for row in (json.load(handle).get("sellers") or [])]
except (OSError, ValueError, KeyError, TypeError) as exc:
    # Not `except OSError` alone, and not a silent `rostered = []`. A malformed document raises
    # a ValueError, a renamed key raises a KeyError, and under `set -euo pipefail` either one
    # was a raw traceback; an ABSENT document set the list empty and skipped the whole trust
    # probe below -- which removed the diagnosis in one of the cases it exists for.
    rostered = []
    problems.append(
        f"cannot read deploy/demo/exchange-deployment.json ({exc}). That document IS the "
        f"exchange's seller registry and its trust_url, so nothing below can be checked and "
        f"the exchange itself is reading the same file."
    )
if rostered:
    snapshot_url = os.environ["TRUST"].rstrip("/") + "/snapshot"
    try:
        with urllib.request.urlopen(snapshot_url, timeout=30) as response:
            snapshot = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, OSError, ValueError) as exc:
        snapshot = None
        unreadable = (
            f"cannot read the trust snapshot at {snapshot_url}: {exc}. The exchange reads that "
            f"same route for R12 and denies EVERY store when it cannot, so this alone empties "
            f"the shortlist."
        )
        # A problem only when the shortlist really is empty, for the reason spelled out below:
        # this probe reaches trust over the host port and the exchange reaches it over the
        # compose network, so the two can legitimately disagree about reachability, and a full
        # shortlist is direct evidence that the exchange's own read is working.
        if slots:
            print(f"NOTE: {unreadable}")
        else:
            problems.append(unreadable)
    if isinstance(snapshot, dict):
        unknown = [store for store in rostered if store not in snapshot]
        blacklisted = [
            store
            for store in rostered
            if isinstance(snapshot.get(store), dict) and snapshot[store].get("blacklisted")
        ]
        print(f"trust snapshot: {len(snapshot)} store(s); {len(rostered) - len(unknown)}/"
              f"{len(rostered)} rostered sellers present")
        # A DIAGNOSIS attached to the shortlist failure, not a failure of its own -- and the
        # distinction is the difference between a probe and a tripwire. R12 excluding SOME
        # stores narrows the field; the demo is only dead when nothing survives, which the
        # `not slots` check above already decides. A store trust has legitimately delisted is
        # the blacklist beat working, and failing the probe for it would make the demo's own
        # feature look like an outage.
        if unknown and not slots:
            problems.append(
                f"...and the live trust snapshot holds no row for {len(unknown)} of the "
                f"{len(rostered)} rostered sellers ({', '.join(unknown[:4])}"
                f"{', ...' if len(unknown) > 4 else ''}). R12 fails closed, so every one of them "
                f"is EXCLUDED from ranking, which is why the shortlist is empty. "
                f"Run `make demo-trust`."
            )
        elif unknown:
            print(
                f"NOTE: {len(unknown)} rostered seller(s) have no trust row and cannot be "
                f"shortlisted ({', '.join(unknown[:4])}); `make demo-trust` registers them"
            )
        if blacklisted:
            print(f"NOTE: trust reports these sellers blacklisted: {', '.join(blacklisted)}")

# EVERY entry being a fallback is the failure this probe exists to catch a second time.
# The graph can find seven shops, the auction can answer 201, and not one hosted agent can
# have bid -- which reads as a working market and is four containers refusing the request.
bidders = [e for e in entries if not e.get("fallback")]
refused = sorted({str(e.get("fallback_reason")) for e in entries if e.get("fallback_reason")})
print(f"hosted bids={len(bidders)}  fallback reasons={refused}")
if not bidders:
    problems.append(
        f"not one rostered store BID -- every entry is a list-price fallback ({refused}). "
        "`store_refused:422` means the agents rejected the BidRequest: the intent this probe "
        "sent is forwarded verbatim and must be a complete `Intent`. "
        "`no_response` on every row means no agent is running -- start the `demo` profile."
    )

if problems:
    print("\nFAIL:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)

print("\nOK: the graph found the candidate set, stores were collected, and the shortlist "
      "is non-empty.")
PY
