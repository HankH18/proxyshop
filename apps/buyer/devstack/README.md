# `apps/buyer/devstack` — the one-command demo stack

Boots the **real** backend for the buyer UI: three real store agents, the real exchange and
the real buyer service, all in one process, all talking to each other over real loopback
sockets. Nothing is mocked and no fixture stands in for a service.

## Run it

```sh
npm run demo --workspace @proxyshop/buyer      # builds the UI, then boots the stack
npm run devstack --workspace @proxyshop/buyer  # boots the stack without rebuilding the UI
```

or by hand, from the repo root:

```sh
PROXYSHOP_WORKER=10 .venv/bin/python apps/buyer/devstack/run.py
```

`--no-open` skips opening a browser. `--port N` (or `BUYER_UI_PORT=N`) moves the buyer app off
its default port **8100**; the store agents and the exchange always bind port 0, because only
this process needs to find them. `--help` lists all of it. Ctrl-C shuts everything down.

If `apps/buyer/dist` does not exist the stack still starts — the API is fully live — and says
loudly that the page is missing and how to build it.

## What is data and what is computed

`demo-market.json` is **the only thing this demo supplies**. It holds merchant data: three
stores' approved envelopes, catalogues, live stock, the platform's trust reading of each, and
the two demo conversations. It holds no shortlist, no ranking, no verdict and no permalink.

Every price, score, exclusion reason, provenance label and `PSX-` code a person sees is
computed at run time by the services themselves. If a store declines, the UI shows the
decline; if the shortlist is empty, the UI shows the exchange's real exclusion reasons.

## The two conversations

Type the turns **exactly** as written. `cluster_id` is a sha256 over the clarified query,
budget band and constraints, and a store agent declines any cluster its envelope does not
pursue — so the wording decides which stores are even asked.

| turns | cluster | what happens |
|---|---|---|
| `looking for a winter hat` then `$80` | `cl-4b6a37aebc537bd5` | woolworks and alpine-supply bid and are shortlisted; fastfleece bids and is **excluded** — `blacklisted_store … may not participate (R12)` |
| `I want a warm merino wool beanie for winter, under $100` | `cl-4d3c3e4edadaa5e7` | woolworks and alpine-supply bid; fastfleece **declines** at its own agent (HTTP 204, `x-proxyshop-decline-reason: no_matching_product`) and the exchange records `fallback: true, fallback_reason: "no_response"` |

Answering the budget question with a hedge — `my budget is about $80` rather than `$80` —
clarifies to a *different* cluster with no hard constraint at all, which no envelope here
pursues. That is the clarifier reading an unresolved answer honestly, not a bug, and it is why
the turns are written out verbatim.

## Two things this launcher does that a deployment document cannot

1. **`configure_solicitation(app, context=…)` per store agent, not `STORE_AGENT_CONTEXT`.**
   That env var is process-wide and names exactly one file, so it cannot describe three stores
   in one process. Each store's own `store_domain` travels in its context, which
   `store_agent.solicitation.serving` documents as outranking the env var.

2. **`configure_ranking(app, catalog=…)` on the exchange.**
   `apps/exchange/src/composition.py` contains the string `catalog` **zero** times, so a
   deployment document has no way to express a catalog snapshot and `configure_exchange` binds
   none. Without this call, measured on this stack: an intent carrying any hard constraint
   gets `ranked: []`, `shortlist.slots: []`, and every candidate lands in `excluded[]` with
   `hard_constraint_unsatisfied` — which reads like a policy decision and is actually a wiring
   hole. Both demo conversations carry hard constraints. This is a defect in the exchange's
   composition root that the launcher reaches past, not a demo convenience; the fix is a
   `catalog` section in the deployment document.

## Files

- `demo-market.json` — the merchant data, and the only data the demo supplies.
- `run.py` — the launcher. Puts the repo root and `.pkgroot` on `sys.path` itself, because it
  is executed by path rather than imported as a package.
