# `pixel/` — the web pixel app extension (T-051)

The emitter half of R4. A shopper completes a checkout, Shopify's Web Pixels sandbox delivers a
`checkout_completed` standard event to this extension, and the extension POSTs four join keys to
the merchant app's collector. SPEC C5 makes this **the only checkout observation path** the system
has, and grants it no protected-customer-data scope.

```
Shopify checkout                      merchant app                      trust
  sandbox  ── checkout_completed ──▶  src/pixel.ts
                                        └─ src/beacon.ts   (flatten, allowlist)
                                        └─ src/transport.ts ─ POST {app_url}/pixel/collect ──▶
                                                              merchant_svc.collector.routes
                                                              → PixelObservation (+ gaps)
```

## The served path

`POST {app_url}/pixel/collect` — `merchant_svc.collector.routes.collect_pixel_event`, mounted by
`merchant_svc.main.create_app`. The URL is not spelled in this workspace: the merchant app hands
it to Shopify at install time as `settings.collectorUrl`, built from
`merchant_svc.install.config.COLLECTOR_PATH` by the same `webPixelCreate` mutation that activates
this extension. `src/settings.ts` reads it back out of `api.settings`.

## What ships this, and what must ship with it

| Artifact | Carries |
| --- | --- |
| `shopify.extension.toml` | the extension manifest — `type = "web_pixel_extension"`, `runtime_context = "strict"`, and the three settings fields. `shopify app deploy` reads it; without it `src/index.ts` is never bundled and never runs. |
| `src/index.ts` | the entry point. `register()` claims `WebPixel::Render`. |
| `@shopify/web-pixels-extension` (devDependency, already in `pixel/package.json`) | types only — nothing from it is in the shipped bundle except the two-line `register` call. |

No new runtime dependency, no new data file. The **one new config surface** is
`[extensions.settings.fields.*]` in `shopify.extension.toml`, and it is not free-floating: Shopify
validates the `settings` object `webPixelCreate` sends against exactly those declared fields, so
the manifest and `webPixelSettings()` in `apps/merchant/app/config/shopify.ts` must agree.
`tests/manifest.test.ts` holds them to each other, because the failure mode otherwise is a merchant
seeing a failed install rather than a red test.

## What the offline tests actually exercise, and what they do not

C9 forbids a live Shopify call, so the boundary is drawn at the `ExtensionApi` object the sandbox
hands `register()`'s callback, and it is drawn honestly.

**Exercised, with the real code:**

- the transform, against the recorded `checkout_completed` event under
  `services/shopify-stub/fixtures/recorded/` — a recording authored from Shopify's published type
  tables by a different lane, so the expected shapes cannot be tuned to match this code;
- the four D24 join keys, held to the recorded `orders/paid` webhook for the *same* purchase, which
  is the property R4 needs and the one a numeric-vs-GID `order_ref` would silently break;
- that no buyer identity reaches the wire when the event carries email, phone, addresses and a
  customer object;
- what the extension subscribes to, and that it is nothing else;
- the `fetch(..., {keepalive: true, credentials: 'omit'})` request the transport builds;
- every failure path: no collector URL, a relative one, an unreachable collector, a refusing one, a
  runtime with no `fetch`, a `fetch` that throws, an unreadable event;
- **and the whole chain end to end** — `apps/merchant/svc/tests/test_collector.py` runs
  `tests/beacon-driver.ts` under `node`, which executes this source unchanged and POSTs over a real
  loopback socket into the real `create_app()` route. The 204 and the stored `PixelObservation`
  come out the far end of the real collector.

**Not exercised, and no test here should be read as claiming otherwise:**

- Shopify's sandbox itself — that it loads the bundle, that `webPixelCreate` accepts this
  manifest's settings, that the event it builds matches the recording;
- whether a browser honours `keepalive` once the tab is closed. That is the browser's contract; all
  that is asserted is that the request asks for it;
- `runtime_context = "strict"` actually denying DOM access. That is Shopify's enforcement.

Those belong to the live dev-store demo procedure (`make e2e-live`), never to a ticket verify.

## Running it

```
npx vitest run pixel          # 39 behaviour tests
npx tsc -b                    # types, including this workspace
npx eslint pixel
PROXYSHOP_WORKER=1 .venv/bin/python -m pytest apps/merchant/svc/tests/test_collector.py -q
```

`node` is a hard requirement of the last one: it runs the real pixel. A missing `node` fails rather
than skips, because a skip would turn "the emitter was never exercised" back into a green run.
