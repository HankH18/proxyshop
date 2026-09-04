# `fixtures/interviews/` — the plain-language interviews envelopes are graded against

R6 says a merchant sets their envelope by answering questions in their own words. Each file
here is one **recorded interview** plus the `contracts.Envelope` that
`merchant_svc.onboarding.envelope_from_transcript` must build from it, so the parse is graded
against a document a human could have said rather than against numbers the parser chose for
itself.

## Shape

The convention is frozen — the acceptance suite reads it directly:

```jsonc
{
  "fixture_version": 1,
  "description": "...",
  "transcript": {                       // or dialogue / messages / turns
    "interview_id": "...",
    "completed_at": "2026-01-04T17:35:00+00:00",   // never the wall clock
    "turns": [
      { "role": "interviewer", "question": "<id>", ...context },
      { "role": "merchant",    "text": "the answer, in sentences" }
    ]
  },
  "expected_envelope": { ... }          // or golden_envelope / envelope
}
```

A sibling pair `<name>.transcript.json` + `<name>.envelope.json` is accepted too. Keep
**exactly one** file carrying both keys: the loader takes the first `*.json` in sorted order
that has them, so a second complete fixture would silently shadow the first.

`expected_envelope` must carry all eight DESIGN fields and `activation == "shadow"` — R7. An
interview never activates anything, whatever the merchant says in it; activation needs a
written approval artifact, which is a separate document.

## The interview script

Only the merchant's side is free text. Each interviewer turn names the `question` it is
asking and carries whatever the app already knows, because the app wrote the questions:

| `question` | context the turn carries | what the answer supplies |
|---|---|---|
| `store` | — | the `<handle>.myshopify.com` host; the handle becomes `store_id` |
| `max_discount_pct` | — | a percentage, in digits or words |
| `budget_cap` | — | a money amount |
| `price_floor` | `product_ref` (`null` = store-wide) | a money amount, or a refusal |
| `pursue_clusters` | `options: [{cluster_id, label}]` | which labels were said yes to |
| `standing_commitment` | `commitment_key`, optional `claim_type` | the promise, in the owner's own words |
| `activation` | — | an acknowledgement that the envelope starts in shadow |

Every one of those questions must be asked and answered. An interview that skipped one is
**refused**, not defaulted: the default for a missing wall is "no wall".

## The one fixture, and what it pins

| file | what it pins |
|---|---|
| `northwind-outfitters.json` | that the answers are genuinely *parsed*. The cap arrives as `"Twenty percent off"` (a number in words), one floor as `"Ninety-five dollars"` and the store-wide floor as `"Nothing under forty dollars"` — a sentence that begins with a refusal word and is not a refusal. One floor question is declined outright (`"No, nothing special there"`) and produces no floor; one commitment is declined and produces no claim. The cluster answer names all three options on screen and rejects the third (`"Camp cooking isn't really us."`), so a substring match would enrol the merchant in a cluster they turned down. |

Every commitment's `provenance.observed_at` is the transcript's own `completed_at`, never the
clock, which is what makes `envelope_from_transcript` byte-for-byte deterministic.

Try it:

```
python -m merchant_svc.onboarding fixtures/interviews/northwind-outfitters.json
```
