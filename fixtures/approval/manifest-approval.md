# Fixture manifest — recorded human approval

- **Approver:** Hank Holcomb
- **Approved at:** 2026-09-03T07:14:09Z
- **Document:** `fixtures/manifest.json`
- **Content digest (sha256 of the manifest body, `approval` excluded):** `0ce80606248b6afe996a5309630359b4bc5fce65fb74d66d3fbc1aae73bffa27`
- **Golden set covered:** `fixtures/golden/golden_set.json` sha256 `0f8c092f0c9ce3935da2e6e6673ee0de50d7c6f9520f79665139b1dc89777012`, 13 pitches

## What this approval covers

SPEC A3: "the trust engine catches the dishonest store" is circular unless the
dishonest behaviours are defined outside the trust engine and approved by a human.
This record is that approval. It covers, as ground truth:

1. **The dishonest store** `store-brightbean` and its 7 scripted behaviours:
   - `bait_and_switch_unit_price` → dimension `price_honored`, observation `contradicted`
   - `phantom_discount` → dimension `discount_honored`, observation `contradicted`
   - `overpromised_dispatch` → dimension `shipped_on_time`, observation `contradicted`
   - `return_window_retracted` → dimension `not_returned`, observation `severe_policy`
   - `misrepresented_ingredients` → dimension `catalog_claim_accuracy`, observation `contradicted`
   - `inflated_specification` → dimension `catalog_claim_accuracy`, observation `contradicted`
   - `pitch_delivery_mismatch` → dimension `feedback_match`, observation `mismatch_return`
2. **The episode budget** (12) and the **new-store prior N** (5).
3. **The blacklist threshold** (0.35) and the **expected trust trajectory**, ending at episode 12 with score 0.073 ± 0.03.
4. **The `claim_type → dimension` table** — typed and exhaustive over the published
   claim-type vocabulary; product-fact types route to `catalog_claim_accuracy` and
   an unmapped claim type raises rather than defaulting (D53).
5. **The golden pitch set**, by digest — its labels are the answer key
   `packages/verification` is graded against and never authors (D11).
6. **The persona scripts** and the fixture intent.

Every digest above was VERIFIED against the approval request before this record was
written; the approval command refuses rather than re-hashing a document that drifted.

## What it does not claim

It does not claim the numbers are optimal, and it is not irreversible: editing any
covered document breaks this digest, and the acceptance suite fails until the
manifest is re-approved. Re-approve; never re-hash.
