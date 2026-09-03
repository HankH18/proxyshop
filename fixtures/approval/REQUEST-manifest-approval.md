# Approval request — `fixtures/manifest.json` (T-080)

**Status: awaiting a human. Nothing here is approved.**

SPEC A3: *"the trust engine catches the dishonest store" is circular if the dishonest
behaviors are defined by the trust engine's own config → behaviors live in a human-approved
fixture manifest.* EXECUTION.md rule 6 adds: *"T-080's manifest approval is a human gate — do
not fabricate the approval artifact."* So the manifest, the golden set and the category
config are written and self-consistent, and the `approval` block is deliberately empty.

## The one command

```
PROXYSHOP_WORKER=1 ./.venv/bin/python -m fixtures.approval --approver "Your Name"
```

`--show` first if you want to see the summary, run the pinned-digest check, and write
nothing. The command writes `fixtures/approval/manifest-approval.md`, fills the `approval`
block in `fixtures/manifest.json`, and prints what it wrote. Commit both.

**The command verifies; it never re-hashes.** Before it writes anything it recomputes every
digest below and compares it against the pins published *in this document*. If any of them
disagree it prints `REFUSED`, names the drifted file with a pinned-vs-on-disk diff, and
writes nothing — because approving a document that changed after you read this request would
record a digest proving only that the file had not changed in the last few milliseconds.

Re-pinning drifted digests is a separate, deliberate pair of commands:

```
./.venv/bin/python -m fixtures.manifest --refresh-digests          # re-pin
./.venv/bin/python -m fixtures.approval --emit-request --write     # re-issue this document
```

The second rewrites only the fenced digest block below — never this prose — so after running
it, read this request again, and read `git diff` on it, before approving.

**Re-pinning does not re-approve, and cannot.** `--refresh-digests` rewrites
`fixtures/manifest.json` only. Your approval is recorded in *two* documents that have to
agree — that file's `approval` block and the committed record it names,
`fixtures/approval/manifest-approval.md`, which quotes the digest — so a re-pin leaves the
record quoting the old digest and the acceptance suite goes red on exactly that
disagreement. Re-issuing this request does not close it either: that rewrites the request,
never the record. The only way back to green is for a human to approve again.

## What approving means

You are declaring these five things to be **ground truth** — the answer key the trust
engine, the simulator, the persona and the verifier are graded *against*, and which none of
them may write:

**1. The dishonest store and its script.** `store-brightbean` lies in seven scripted ways,
replayed once per simulation episode:

| `kind` | dimension | observation |
| --- | --- | --- |
| `bait_and_switch_unit_price` | `price_honored` | `contradicted` |
| `phantom_discount` | `discount_honored` | `contradicted` |
| `overpromised_dispatch` | `shipped_on_time` | `contradicted` |
| `return_window_retracted` | `not_returned` | `severe_policy` |
| `misrepresented_ingredients` | `catalog_claim_accuracy` | `contradicted` |
| `inflated_specification` | `catalog_claim_accuracy` | `contradicted` |
| `pitch_delivery_mismatch` | `feedback_match` | `mismatch_return` |

All six trust dimensions are exercised, including the sixth (`catalog_claim_accuracy`, D53):
the store lies about *the goods*, not only about *the deal*.

**2. The numbers.** Episode budget **12**; new-store prior **N = 5** clean episodes;
blacklist threshold **0.35**; neutral prior Beta(2,2); half-life 30 days; observation weights
`contradicted 2.0`, `severe_policy 3.0`, `mismatch_return 1.5`, `unsupported 0.5`,
`ambiguous 0.0`.

**3. The expected trust trajectory.** 0.50 at episode 0 → **0.13 ± 0.05 at episode 12**, i.e.
below the 0.35 threshold even at the top of the tolerance band, inside the budget. That is
the S2 claim, stated in the manifest rather than discovered by the engine. *This is the
number most worth your scrutiny: the trust engine (T-062) does not exist yet, so this
trajectory is an approved expectation, and T-062 will be graded against it.*

**4. The `claim_type → dimension` table.** Fourteen published claim types, each mapped to
exactly one of the six dimensions; the four product-fact types (`ingredients`,
`compatibility`, `nutrition`, `specifications`) route to `catalog_claim_accuracy`; the ten
offer-integrity types keep their transaction dimension; `feedback_match` takes no
verification outcome at all. An unmapped claim type **raises** at load; it never defaults.

**5. The golden pitch set** — 13 pitches, 36 labelled claims, one case for each of the eleven
DESIGN eval gates, all four verification statuses, one pitch carrying all four at once, and
**each of the four product-fact claim types (`ingredients`, `compatibility`, `nutrition`,
`specifications`) labelled both true and false.** That last part includes the manifest's own
flagship catalog lie: `gp-012-misrepresented-ingredients` claims "100% single-origin arabica"
for a product the snapshot records as an arabica/robusta blend, which is the scripted
`misrepresented_ingredients` behaviour in row 5 of the table above. This is what
`packages/verification` is scored against, so its labels decide whether the verifier is right
or wrong.

Plus the persona scripts (`aggressive`, `honest`) and the fixture intent.

## The digests you are approving

These are the exact bytes. The approval command recomputes all three and refuses unless they
still match, so what you read here is what gets approved — nothing more:

```
fixtures/manifest.json           0ce80606248b6afe996a5309630359b4bc5fce65fb74d66d3fbc1aae73bffa27
fixtures/golden/golden_set.json  0f8c092f0c9ce3935da2e6e6673ee0de50d7c6f9520f79665139b1dc89777012
fixtures/catalog/coffee.json     4a94895f4209b30d2a96296072e44222245ee44c0fc7e556c700233258cb20d7
```

The first is the sha256 of the manifest **body** — the document with its own `approval` block
removed, serialized `sort_keys=True, separators=(",", ":")`. `golden_set.sha256` and
`seed_catalog.sha256` sit inside that body, so approving the manifest transitively approves
the exact bytes of the other two. Editing any of the three afterwards breaks the digest and
the acceptance suite fails until it is **re-approved** — re-approve, never re-hash.

Check them yourself before you type the command, if you like:

```
./.venv/bin/python -m fixtures.manifest          # read-only: pinned vs on disk
shasum -a 256 fixtures/golden/golden_set.json fixtures/catalog/coffee.json
```

## What it does not mean

It is not a claim that the numbers are optimal, and it is not irreversible: re-pin, re-issue
this request, read it, and approve the new document instead. It is also not a guarantee a
human did it — no offline test can tell a signature from an agent typing a name. The
authenticity of the artifact rests on this gate, not on any green test.

## What is blocked until then

`test_e8_proofs.py::test_fixture_manifest_carries_a_recorded_human_approval_artifact` fails
with `manifest.approval.approver must be <class 'str'>, got NoneType` — the honest failure of
an unapproved document. Sixteen tickets sit behind this gate (T-032, T-034, T-035, T-045,
T-054, T-062, T-063, T-064, T-065, T-073, T-081, T-082, T-083, T-084, T-085 and the S8
release-blocker work that reads the golden set).

Everything else in T-080 is built, committed and green: the manifest, the golden set, the
category config, the generator, `make demo-seed`, and the tests.
