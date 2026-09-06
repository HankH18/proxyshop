/**
 * The walk over a claim's opaque `value`, in the door the exchange actually serves in Node.
 *
 * The twin of `test_boundary_claim_value_walk.py`, case for case. It exists because the Python
 * file was written first and, for a while, was the only thing defending any of these properties:
 * every one of them could be deleted from `boundary.ts` with all 713 TypeScript tests still
 * green. A dual-language boundary whose properties are asserted in one language only is a
 * boundary with one door guarded.
 *
 * The cross-language claim is not "both files have tests"; it is that these particular payloads
 * get the SAME verdict from both doors. Each `expect` here has a byte-identical counterpart in
 * the Python file.
 */
import {describe, expect, it} from "vitest";

import {
  CLAIM_VALUE_MAX_DEPTH,
  CLAIM_VALUE_MAX_ENTRIES,
  EXTERNAL_PATH,
  HOSTED_PATH,
  REASON_CLAIM_VALUE_UNWALKABLE,
  REASON_HOSTED_NON_HOOK_PROVENANCE,
  validateBid,
} from "../src/ts/boundary.js";
import {
  ASSERTED_PROVENANCE,
  HOOK_PROVENANCE,
  type Json,
  makeBid,
  makeClaim,
  makeOffer,
  makeSnapshotTable,
} from "./fixtures.js";

const NOW = "2026-06-01T00:00:00Z";

/**
 * The same honest roster the Python twin uses: it prices the fixture offer's product and
 * authorises more depth than any bid here declares, so the price wall refuses nothing and the
 * only thing that can refuse a bid in this file is the walk under test.
 */
const HONEST_ROSTER = {"prod-1": {list_price: 49.0, max_discount_pct: 25.0}};

function check(bid: unknown, path: string = HOSTED_PATH) {
  return validateBid(bid, {
    path,
    trustSnapshot: makeSnapshotTable(),
    now: NOW,
    listPrices: HONEST_ROSTER,
  } as never);
}

function withValue(value: unknown, path: string = HOSTED_PATH) {
  const claim = {key: "policy", value, provenance: {...HOOK_PROVENANCE}};
  return check(makeBid({claims: [claim]}), path);
}

function wrap(depth: number, inner: unknown): unknown {
  let value = inner;
  for (let i = 0; i < depth; i += 1) value = {a: value};
  return value;
}

function unwalkable(result: {reasons: string[]}): string[] {
  return result.reasons.filter((reason) => reason.startsWith(REASON_CLAIM_VALUE_UNWALKABLE));
}

const asserted = (): Json => ({...ASSERTED_PROVENANCE}) as Json;

describe("the claim-value walk is armed", () => {
  it("admits an ordinary bid and refuses a top-level seller_asserted block", () => {
    expect(check(makeBid()).ok, check(makeBid()).reasons.join(", ")).toBe(true);

    const refused = check(makeBid({claims: [makeClaim("x", "y", ASSERTED_PROVENANCE)]}));
    expect(refused.ok).toBe(false);
    expect(refused.reasons).toContain(`${REASON_HOSTED_NON_HOOK_PROVENANCE}:0:seller_asserted`);
  });
});

describe("a provenance block is recognised wherever it is written", () => {
  const shapes: Array<[string, unknown]> = [
    ["under a provenance key", {provenance: asserted()}],
    ["wrapped in a list", {provenance: [asserted()]}],
    ["wrapped in two lists", {provenance: [[asserted()]]}],
    ["a record inside a list", {provenance: [{x: asserted()}]}],
    ["under a numeric-looking key", {provenance: {"0": asserted()}}],
    ["under any other key at all", {x: {y: asserted()}}],
    ["as a bare list element", [asserted()]],
    ["as the whole value", asserted()],
    ["six wrappers down", wrap(6, {provenance: asserted()})],
    ["sixty wrappers down", wrap(60, {provenance: asserted()})],
  ];

  it.each(shapes)("refuses it on the hosted path: %s", (_shape, value) => {
    const result = withValue(value);
    expect(result.ok, result.reasons.join(", ")).toBe(false);
    expect(result.reasons.some((r: string) => r.startsWith(REASON_HOSTED_NON_HOOK_PROVENANCE))).toBe(
      true,
    );
  });

  it("admits and flags the same block on the external path", () => {
    const result = withValue({provenance: asserted()}, EXTERNAL_PATH);
    expect(result.ok, result.reasons.join(", ")).toBe(true);
    expect(result.requires_verification).toBe(true);
    expect(result.unverified_claim_indexes).toEqual([0]);
  });
});

describe("an honest provenance block is not a lid the walk stops at", () => {
  /**
   * The failure mode recognising-by-shape creates, and the one that cost the most to find. The
   * recogniser used to report a block and stop, which was safe while it only fired under a key
   * literally named `provenance`. Once it fired on shape, one added key turned any mapping into a
   * recognised — and perfectly innocent — block, and everything underneath became a region the
   * walk never entered. That reopened T-161 and T-162 on BOTH doors with both suites green.
   */
  const lids: Array<[string, unknown]> = [
    ["a bare source key", {source: "network", x: {provenance: asserted()}}],
    ["a different hook source", {source: "scraped", x: {provenance: asserted()}}],
    ["an entirely honest block", {...HOOK_PROVENANCE, x: {provenance: asserted()}}],
    ["a lid one wrapper down", {w: {source: "network", x: {provenance: asserted()}}}],
    ["two lids", {source: "network", a: {source: "scraped", b: {provenance: asserted()}}}],
    ["a lid over a list", {source: "network", x: [asserted()]}],
  ];

  it.each(lids)("still finds what is under it: %s", (_shape, value) => {
    const result = withValue(value);
    expect(result.ok, result.reasons.join(", ")).toBe(false);
    expect(result.reasons.some((r: string) => r.startsWith(REASON_HOSTED_NON_HOOK_PROVENANCE))).toBe(
      true,
    );
  });

  it("does not hide a discount authorisation either", () => {
    const result = withValue({source: "network", x: {authorized_discount_pct: 25.0}}, EXTERNAL_PATH);
    expect(result.ok === false || result.requires_verification === true).toBe(true);
  });
});

describe("a legitimate structured value is still admitted", () => {
  const benign: Array<[string, unknown]> = [
    ["prose under a provenance key", {provenance: "we read it off the label"}],
    ["a source outside the vocabulary", {provenance: {source: "our CRM"}}],
    ["a number under a provenance key", {provenance: 7}],
    ["a hook source, nested", {provenance: {...HOOK_PROVENANCE}}],
    ["an ordinary structured value", {colour: "blue", sizes: ["s", "m"], n: {x: 1}}],
    // `Provenance.source` is a string enum in the published bundle, so a list-valued source is
    // caller data. This door used to read `String(["seller_asserted"])` as `"seller_asserted"`
    // while Python read it as `"['seller_asserted']"` — one payload, two doors, two answers.
    ["a source that is a list", {provenance: {source: ["seller_asserted"]}}],
    ["a source that is a number", {provenance: {source: 7}}],
  ];

  it.each(benign)("admits it: %s", (_shape, value) => {
    const result = withValue(value);
    expect(result.ok, result.reasons.join(", ")).toBe(true);
  });
});

describe("the bounds fail closed, and are wide enough that nothing honest reaches them", () => {
  it("pins the two numbers, so moving either is a deliberate act", () => {
    // Identical to the Python peer. Two doors that give up in different places admit different
    // bids, and nothing else in either suite constrains these.
    expect(CLAIM_VALUE_MAX_DEPTH).toBe(64);
    expect(CLAIM_VALUE_MAX_ENTRIES).toBe(65536);
  });

  it("refuses rather than admits when padding would exhaust the walk", () => {
    const value: Record<string, unknown> = {};
    for (let i = 0; i <= CLAIM_VALUE_MAX_ENTRIES; i += 1) {
      value[`a${String(i).padStart(7, "0")}`] = {provenance: {...HOOK_PROVENANCE}};
    }
    value["zzzz"] = {provenance: asserted()};

    const result = withValue(value);
    expect(result.ok, "padding bought a way past a wall that fails open").toBe(false);
    expect(unwalkable(result).length).toBeGreaterThan(0);
  });

  it("refuses rather than admits when nesting would outrun the depth bound", () => {
    const result = withValue(wrap(CLAIM_VALUE_MAX_DEPTH + 5, {provenance: asserted()}));
    expect(result.ok).toBe(false);
    expect(unwalkable(result).length).toBeGreaterThan(0);
  });

  const nothingSkipped: Array<[string, unknown]> = [
    ["an empty object", {}],
    ["an empty array", []],
    ["a string", "30 days"],
    ["a number", 7],
    ["null", null],
    // The leaf sits one step PAST the depth bound in each of these three, with every wrapper
    // inside it. The only thing declined is a leaf that had nothing in it.
    ["an empty object one step past the bound", wrap(CLAIM_VALUE_MAX_DEPTH + 1, {})],
    ["an empty array one step past the bound", wrap(CLAIM_VALUE_MAX_DEPTH + 1, [])],
    ["a scalar one step past the bound", wrap(CLAIM_VALUE_MAX_DEPTH + 1, "x")],
  ];

  it.each(nothingSkipped)("does not claim truncation when nothing was skipped: %s", (_s, value) => {
    const result = withValue(value);
    expect(unwalkable(result)).toEqual([]);
    expect(result.ok, result.reasons.join(", ")).toBe(true);
  });

  it("judges a very deep chain rather than blowing the stack", () => {
    // The walk is iterative, so the depth bound is a policy rather than a stand-in for the
    // engine's call-stack limit.
    const result = withValue(wrap(10000, {provenance: asserted()}));
    expect(result.ok).toBe(false);
    expect(unwalkable(result).length).toBeGreaterThan(0);
  });

  it("costs less to enforce the bound than to ignore it", () => {
    // The payload is REJECTED, and that is the point: an earlier version read and sorted the
    // children of every node the budget had already refused, so the more hostile the payload the
    // more work it bought.
    const value: Record<string, unknown> = {};
    for (let i = 0; i < 60000; i += 1) value[`k${i}`] = {v: i, w: i};

    const started = performance.now();
    const result = withValue(value);
    const elapsed = performance.now() - started;

    expect(unwalkable(result).length, "the payload no longer exceeds the budget").toBeGreaterThan(0);
    expect(elapsed).toBeLessThan(500);
  });

  it("declines a million-key object without allocating its keys", () => {
    // `Object.keys(record).length` cost 153 ms here for the same verdict Python reached in 0.0 ms.
    const value: Record<string, unknown> = {};
    for (let i = 0; i < 1000000; i += 1) value[`k${i}`] = i;

    const started = performance.now();
    const result = withValue(value);
    const elapsed = performance.now() - started;

    expect(unwalkable(result).length).toBeGreaterThan(0);
    expect(elapsed).toBeLessThan(500);
  });
});

describe("an external claim of one's own discount authority is never taken on trust", () => {
  const shapes: Array<[string, Json]> = [
    ["as the claim key", {key: "authorized_discount_pct", value: 20.0}],
    ["in the value", {key: "policy", value: {authorized_discount_pct: 25.0}}],
    ["the envelope's spelling", {key: "policy", value: {max_discount_pct: 25.0}}],
    ["deep in the value", {key: "policy", value: wrap(10, {max_discount_pct: 1.0}) as Json}],
    ["inside a list", {key: "policy", value: {g: [{authorized_discount_pct: 1.0}]}}],
  ];

  it.each(shapes)("refuses or flags it: %s", (_shape, claim) => {
    const payload = {...claim, provenance: {...HOOK_PROVENANCE}};
    const result = check(makeBid({claims: [payload]}), EXTERNAL_PATH);
    expect(result.ok === false || result.requires_verification === true).toBe(true);
  });

  it("never starves the authorisation check while the walk still finishes", () => {
    // The provenance question and the authorisation question are ONE walk. As two walks with two
    // budgets they drifted apart — the provenance walk stops at a recognised block, the
    // authorisation walk descended into it — and a measured 255-payload-wide window admitted an
    // external submission carrying `authorized_discount_pct` with nothing said.
    for (const padding of [0, 255, 256, 510, 511, 2000]) {
      const value: Record<string, unknown> = {};
      for (let i = 0; i < padding; i += 1) {
        value[`a${String(i).padStart(5, "0")}`] = {provenance: {...HOOK_PROVENANCE}};
      }
      value["zzzz"] = {authorized_discount_pct: 25.0};

      const result = withValue(value, EXTERNAL_PATH);
      expect(
        result.ok === false || result.requires_verification === true,
        `padding=${padding}: admitted with nothing said — ${result.reasons.join(", ")}`,
      ).toBe(true);
    }
  });

  it("does not second-guess the hosted path about authorisation", () => {
    const result = check(makeBid({claims: [makeClaim("authorized_discount_pct", 20.0)]}), HOSTED_PATH);
    expect(result.ok, result.reasons.join(", ")).toBe(true);
    expect(result.requires_verification).toBe(false);
  });

  it("reads a padded key the same way the Python door reads it", () => {
    // `String.prototype.trim()` and `str.strip()` are different functions — Python strips
    // U+001C-U+001F and U+0085, `trim` strips U+FEFF — so a key padded with one of the six was
    // read by one door and not the other. Both now use the union, spelled by code point.
    for (const pad of ["﻿", "", "", "", "", "", " ", "　"]) {
      const claim = makeClaim(`${pad}authorized_discount_pct${pad}`, 20.0);
      const result = check(makeBid({claims: [claim]}), EXTERNAL_PATH);
      expect(
        result.ok === false || result.requires_verification === true,
        `a key padded with U+${pad.codePointAt(0)!.toString(16)} slipped past`,
      ).toBe(true);
    }
  });
});

describe("one payload, one verdict", () => {
  it.each([
    [["zzz", "aaa", "mmm"]],
    [["mmm", "zzz", "aaa"]],
  ])("reports in key order however the object was written: %s", (written) => {
    // `Object.entries` hoists integer-like keys to the front; Python walks insertion order. Two
    // doors reading one object in two orders is how a budget that decides what gets seen turned
    // identical bytes into different verdicts. Fail-closed truncation removed the verdict half of
    // that; the canonical order is what keeps the two doors telling the same story.
    const value: Record<string, unknown> = {};
    for (const key of written as string[]) value[key] = {provenance: asserted()};

    const result = withValue(value);
    expect(result.ok).toBe(false);
    const reported = result.reasons
      .filter((r: string) => r.startsWith(REASON_HOSTED_NON_HOOK_PROVENANCE))
      .map((r: string) => r.split(":")[1]);
    expect(reported).toEqual([
      "0.value.aaa.provenance",
      "0.value.mmm.provenance",
      "0.value.zzz.provenance",
    ]);
  });

  it("finds a block hidden at an offer commitment too", () => {
    const commitment = {key: "x", value: {provenance: asserted()}, provenance: {...HOOK_PROVENANCE}};
    const result = check(
      makeBid({claims: [makeClaim("a", "b")], offer: makeOffer({commitments: [commitment]})}),
    );
    expect(result.ok, result.reasons.join(", ")).toBe(false);
  });
});

describe("the walk never throws", () => {
  const hostile: Array<[string, unknown]> = [
    ["a source getter that throws", {provenance: {get source() {
      throw new Error("source getter exploded");
    }}}],
    ["a source whose toString throws", {provenance: {source: {toString() {
      throw new Error("toString exploded");
    }}}}],
    ["a self-referential object", (() => {
      const node: Record<string, unknown> = {};
      node["provenance"] = node;
      return node;
    })()],
    ["a prototype-shaped key", JSON.parse('{"__proto__": {"provenance": {"source": "seller_asserted"}}}')],
    ["a getter that throws mid-walk", {a: {get b() {
      throw new Error("b exploded");
    }}}],
  ];

  it.each(hostile)("returns a verdict rather than throwing: %s", (_shape, value) => {
    for (const path of [HOSTED_PATH, EXTERNAL_PATH]) {
      expect(() => withValue(value, path)).not.toThrow();
      expect(typeof withValue(value, path).ok).toBe("boolean");
    }
  });
});
