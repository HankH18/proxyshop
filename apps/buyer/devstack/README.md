# `apps/buyer/devstack` — the one-command demo stack

Boots the **real** backend for the buyer UI: three real store agents, the real exchange and
the real buyer service, all in one process, all talking to each other over real loopback
sockets. Nothing is mocked and no fixture stands in for a service.

## Run it

```sh
npm run demo --workspace @proxyshop/buyer      # builds the UI, then boots the stack
npm run devstack --workspace @proxyshop/buyer  # the same thing under a different name
```

Both build first. `apps/buyer/dist` is gitignored, so a checkout has no built page until
something builds one, and a launcher that skips the build serves the API plus a "THE UI IS NOT
BUILT" banner — which reads like a broken demo. `npm run devstack:nobuild` is the one that
skips it, for work on the API where the page does not matter.

Or by hand, from the repo root — the build is then yours to run:

```sh
npm run build:ui --workspace @proxyshop/buyer
PROXYSHOP_WORKER=10 .venv/bin/python apps/buyer/devstack/run.py
```

`--no-open` skips opening a browser. `--port N` (or `BUYER_UI_PORT=N`) moves the buyer app off
its default port **8100**; the store agents and the exchange always bind port 0, because only
this process needs to find them. `--help` lists all of it. Ctrl-C shuts everything down.

If `apps/buyer/dist` does not exist the stack still starts — the API is fully live — and says
loudly that the page is missing and how to build it.

## Signing in, which the journey requires and a workstation cannot mail

Step 2's confirm button is **absent from the page** until there is a session (`Journey.tsx`,
`gateOnSignIn`): confirming is the step that leaves this origin, and the pseudonym the stores
are told has to come from the buyer service's vault rather than from the browser. A session
comes from redeeming an emailed single-use link, and a laptop has no MTA — with none
configured `POST /buyer/auth/magic-link` answers `503` on purpose, which is correct and also a
dead end for the demo.

So this launcher sets two variables before it creates the buyer app:

- `PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT=console` — the explicitly-named local transport that
  **prints the sign-in link to this terminal** instead of mailing it. Nothing else reaches it:
  unset, blank, or misspelled is a refusal, so the fail-closed default is untouched for every
  deployment that is not this launcher.
- `PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL=http://127.0.0.1:8100/` — the origin *this* process
  serves the page on. `.env.example` ships `http://localhost:8081/`, which is right for
  `apps/buyer/compose.yaml` and wrong here by a port, and a link built from the wrong port is a
  404 rather than a sign-in.

Set either variable yourself — a real MTA, say — and the launcher leaves both alone.

Type any address into the page's sign-in panel (no mailbox has to exist), then open the
`?token=` URL that appears in the terminal. **Do it before typing the conversation:** opening
the link reloads the page, and a conversation started first is gone.

That printed token is a live bearer credential. Anything that can read this process's stdout
can sign in as whoever asked for a link, which is fine for one terminal on one workstation and
is an authentication bypass anywhere stdout is collected. The transport says so at boot.

## What is data and what is computed

`demo-market.json` is **the only thing this demo supplies**. It holds merchant data: three
stores' approved envelopes, catalogues, live stock, the platform's trust reading of each, and
the two demo conversations. It holds no shortlist, no ranking, no score, no exclusion reason,
no verdict and no permalink.

It *does* hold **prices** — `list_price` 78.00 / 72.00 / 45.00, the envelope `floors`, and the
budgets in the two conversations — because those are the merchant's catalogue and the buyer's
budget, which is exactly the input a market file is for. The bid prices that come back happen
to be the same three numbers, and that is not a copy: the store agent runs its pricing pass
over its envelope and, with no authorized discount, `store_agent.modes.runner` records *"no
authorized discount: the catalog list price stands"*. All three stores here are a cold start
(`learned_policy: null`), so every pass ends there. Give one a learned policy and the same
file yields a different `unit_price`.

Every score, exclusion reason, provenance label and `PSX-` code a person sees is computed at
run time by the services themselves. If a store declines, the UI shows the decline; if the
shortlist is empty, the UI shows the exchange's real exclusion reasons.

## The two conversations

Type the turns **exactly** as written. `cluster_id` is a sha256 over the clarified query,
budget band and constraints, and a store agent declines any cluster its envelope does not
pursue — so the wording decides which stores are even asked.

| turns | further answers | cluster | what happens |
|---|---|---|---|
| `looking for a winter hat` then `$80` | **2** | `cl-4b6a37aebc537bd5` | woolworks and alpine-supply bid and are shortlisted; fastfleece bids and is **excluded** — `blacklisted_store … may not participate (R12)` |
| `I want a warm merino wool beanie for winter, under $100` | **0** | `cl-4d3c3e4edadaa5e7` | woolworks and alpine-supply bid; fastfleece **declines** at its own agent (HTTP 204, `x-proxyshop-decline-reason: no_matching_product`) and the exchange records `fallback: true, fallback_reason: "no_response"` |

**"Further answers" is not decoration.** R1 caps the clarifier at three questions and it keeps
asking until it has asked them, and *every answer you type is another turn in the cluster hash*.
Answer exactly the stated number and no more. Measured against a running buyer service:
winter-hat holds `cl-4b6a37aebc537bd5` through two extra `no`s and moves to
`cl-d35bd50812d2d9a0` on a third; merino-beanie needs none at all — its confirm screen is up
immediately — and a single extra `no` moves it to `cl-e437e6d0c763d905`. Neither drifted
cluster is in any envelope's `pursue_clusters`, so every store declines and the page goes empty
for a reason that is the reader's keystrokes rather than the market's. `run.py` prints the
count for each conversation, read from `conversations[].further_answers`.

Answering the budget question with a hedge — `my budget is about $80` rather than `$80` —
clarifies to a *different* cluster with no hard constraint at all, which no envelope here
pursues. That is the clarifier reading an unresolved answer honestly, not a bug, and it is why
the turns are written out verbatim.

## What this launcher does that a deployment document cannot

1. **`configure_solicitation(app, context=…)` per store agent, not `STORE_AGENT_CONTEXT`.**
   That env var is process-wide and names exactly one file, so it cannot describe three stores
   in one process. Each store's own `store_domain` travels in its context, which
   `store_agent.solicitation.serving` documents as outranking the env var. Unlike (2) this is
   **not** a hole in the store agent's deployment format: a real deployment runs one store per
   process and says all of this in `STORE_AGENT_CONTEXT`. It is unreachable-by-document here
   only because this launcher deliberately puts three merchants in one process.

That is the only one left. There used to be a second, and it is recorded here rather than
deleted because the launcher **still makes the call** and a reader will find it:

2. **`configure_ranking(app, catalog=…)` on the exchange — the hole this reached past is
   closed.** It was a real one: `apps/exchange/src/composition.py` contained the string
   `catalog` zero times, so a deployment document had no way to express a catalog snapshot and
   `configure_exchange` bound none. It now names `catalog` on 32 lines — the document has a
   `catalog` key, `_catalog()` validates it, and `configure_exchange` binds it through
   `configure_ranking` (`composition.py:1952`). What has not changed is the *consequence* of
   binding no catalog, measured on this stack: an intent carrying any hard constraint gets
   `ranked: []`, `shortlist.slots: []`, and every candidate lands in `excluded[]` with
   `hard_constraint_unsatisfied` — which reads like a policy decision and is actually an
   unwired verifier. Both demo conversations carry hard constraints, so this launcher must
   still bind one; it can now do it the way a deployment would, and the comment at the call
   site in `run.py` still calls it a hole in the composition root.

## Files

- `demo-market.json` — the merchant data, and the only data the demo supplies.
- `run.py` — the launcher. Puts the repo root and `.pkgroot` on `sys.path` itself, because it
  is executed by path rather than imported as a package.
