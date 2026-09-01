#!/usr/bin/env bash
# Fail EARLY and readably on the four things that make an unattended run die in the middle.
# Orchestrator-owned (T-000), frozen.
#
#   1. one of the compose host ports is already occupied   (D9)
#   2. docker reports less than 4 GiB of memory             (D10)
#   3. PROXYSHOP_WORKER is unset                            (D38)
#   4. `node` on PATH is older than 22 — this host has a stale /usr/local/bin/node v18
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"

MIN_DOCKER_BYTES=$((4 * 1024 * 1024 * 1024))
fail() { echo "PREFLIGHT FAIL: $*" >&2; exit 1; }

# --- 3. worker identity (checked first: it is the cheapest and the most often forgotten)
[ -n "${PROXYSHOP_WORKER:-}" ] || fail "PROXYSHOP_WORKER is unset (D38). Export it, e.g. PROXYSHOP_WORKER=1."

# --- 1. ports
port_busy() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }
for spec in \
  "postgres:${PG_PORT:-5432}" \
  "redis:${REDIS_PORT:-6379}" \
  "neo4j-http:${NEO4J_HTTP_PORT:-7474}" \
  "neo4j-bolt:${NEO4J_BOLT_PORT:-7687}"
do
  name="${spec%%:*}"; port="${spec##*:}"
  if port_busy "$port"; then
    if docker compose ps --format '{{.Service}}' 2>/dev/null | grep -qx "$name"; then
      echo "    port $port ($name) is held by this project's own stack — fine"
    else
      fail "port $port is occupied but is not ProxyShop's $name. Free it, or override the port: ${name}=... in .env"
    fi
  fi
done

# --- 2. docker memory
docker info >/dev/null 2>&1 || fail "docker is not responding. Start Docker Desktop."
mem_total="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
[ "$mem_total" -ge "$MIN_DOCKER_BYTES" ] || fail "docker reports ${mem_total} bytes of memory; ProxyShop needs at least 4 GiB (D10)."

# --- 4. node major
node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
[ "$node_major" -ge 22 ] || fail "node major version is ${node_major}; ProxyShop needs >= 22. A stale /usr/local/bin/node may be winning on PATH — check \`which -a node\`."

echo "OK: preflight (worker ${PROXYSHOP_WORKER}, docker ${mem_total} bytes, node ${node_major}.x)"
