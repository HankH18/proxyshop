# `fixtures/envelopes/` — approved envelopes the discount walls are graded against

Each file here is one **approved** `contracts.Envelope` plus the catalog it prices against and a
table of discount requests with the answer the envelope's own arithmetic gives. They exist so
T-040's acceptance criterion 2 — *"`authorize_discount` denies below-floor / over-cap against
approved envelope fixtures"* — is checked against data a human approved, not against numbers the
implementation chose for itself.

## Shape

```jsonc
{
  "fixture_version": 1,
  "description": "...",                 // what this envelope is for
  "approved_by": "...",                 // the approval this stands in for
  "envelope":   { ... },                // validated with contracts.Envelope — extra keys forbidden
  "catalog":    { "<product_ref>": { "product_ref", "list_price", ... } },
  "discount_expectations": [
    { "product_ref": "...", "requested_pct": 20.0, "outcome": "granted" },
    { "product_ref": "...", "requested_pct": 25.0, "outcome": "denied",
      "reason": "over_max_discount_pct", "note": "why, in arithmetic" }
  ]
}
```

`reason` is one of the `REASON_*` constants in `store_agent.hooks.tools`, so a renamed denial
reason breaks the fixture rather than silently passing under a new spelling.

## The two envelopes, and why both

| file | what it pins |
|---|---|
| `store-alpha.approved.json` | the two walls **independently**: a request refused by the cap while comfortably above the floor, and a request refused by a *per-product* floor while comfortably inside the cap. Both boundaries are exercised exactly on the line (`20.0` at a 20 % cap is granted; `5.0` landing exactly on a 95.00 floor is granted) and one step past it. |
| `store-beta.approved.json` | the **store-wide** floor (`"product_ref": null`), and that a per-product floor and the store-wide floor compose to the *stricter* of the two — `prod-tight` is refused at 40 % by its own 48.00 floor even though the store-wide 40.00 floor would have allowed it. |

Every expectation is derived from the envelope by hand:
`resulting_price = list_price × (100 − requested_pct) / 100`, refused if it lands under
`max(store_wide_floor, product_floor)` or if `requested_pct` exceeds `max_discount_pct`.
A limit the merchant approved is a limit that may be *reached*, so landing exactly on the cap or
exactly on the floor is authorized.
