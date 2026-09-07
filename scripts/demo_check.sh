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
QUERY="${DEMO_QUERY:-milk thistle silymarin liver support extract}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "FATAL: python3 is not on PATH." >&2
  exit 2
fi

echo "opening an auction on ${EXCHANGE}/auctions with NO roster in the body ..."
echo "  intent query: ${QUERY}"
echo

EXCHANGE="$EXCHANGE" QUERY="$QUERY" python3 - <<'PY'
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
