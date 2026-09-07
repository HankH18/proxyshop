/**
 * T-204, from the TypeScript side: the 409 `denial_reason` vocabulary and its parser.
 *
 * The Python half is `test_denial_vocabulary.py`. It re-derives every row of
 * `denial_reason_corpus.json` from `exchange.accept.reasons.denial_code`, so the table below is
 * the exchange's own answers rather than a second opinion — a row cannot be edited to make this
 * file green without turning that one red.
 *
 * The rows that earn the corpus are the blank ones. The exchange strips the token with Python's
 * `str.strip()`; `trim()` disagrees in both directions — it keeps U+001C–U+001F and U+0085, and
 * it removes U+FEFF. A client that implements the document's "parse the token before the first
 * colon" with `split(":")[0].trim()` therefore mislabels real refusals, which is exactly why the
 * parser is published rather than described.
 */
import {describe, expect, it} from "vitest";

import {protocolSchema} from "../src/ts/schemas.js";
import {DENIAL_CODES, denialCode} from "../src/ts/vocabulary.js";
import corpus from "./denial_reason_corpus.json" with {type: "json"};

const rows = corpus.rows as ReadonlyArray<{reason: string; code: string | null}>;

describe("the published denial-code vocabulary", () => {
  it("is exactly the bundle's DenialCode enum", () => {
    const published = (protocolSchema.$defs.DenialCode as {enum: string[]}).enum;
    expect([...DENIAL_CODES].sort()).toEqual([...published].sort());
  });

  it("is nine codes and carries the `unspecified` fail-safe", () => {
    expect(DENIAL_CODES).toHaveLength(9);
    expect(DENIAL_CODES).toContain("unspecified");
    expect(DENIAL_CODES).toContain("blacklisted");
  });
});

describe("denialCode agrees with the exchange's own parser on every corpus row", () => {
  it("has a corpus worth running", () => {
    expect(rows.length).toBeGreaterThanOrEqual(24);
    expect(rows.some((row) => row.code === null)).toBe(true);
    expect(new Set(rows.map((row) => row.code)).size).toBeGreaterThanOrEqual(6);
  });

  it("reads the same code out of every row", () => {
    const wrong = rows
      .map((row) => ({...row, got: denialCode(row.reason)}))
      .filter((row) => row.got !== row.code);
    expect(wrong).toEqual([]);
  });

  it("is not `split(':')[0].trim()`, and the corpus proves it", () => {
    const naive = (reason: string): string | null => {
      const token = (reason.split(":")[0] ?? "").trim();
      return (DENIAL_CODES as readonly string[]).includes(token) ? token : null;
    };
    const disagreements = rows.filter((row) => naive(row.reason) !== row.code);
    // If this ever reaches zero the corpus has lost the rows that separate the two rules, and
    // this file would be passing a test that can no longer fail.
    expect(disagreements.length).toBeGreaterThanOrEqual(5);
    for (const row of disagreements) {
      expect(denialCode(row.reason)).toBe(row.code);
    }
  });
});

describe("denialCode on inputs no corpus row can carry", () => {
  it("answers null for values that are not strings at all", () => {
    expect(denialCode(null)).toBeNull();
    expect(denialCode(undefined)).toBeNull();
    expect(denialCode(0)).toBeNull();
    expect(denialCode(false)).toBeNull();
    expect(denialCode({})).toBeNull();
  });

  it("reads the code out of a value that stringifies to one", () => {
    expect(denialCode({toString: () => "blacklisted: chargeback fraud"})).toBe("blacklisted");
  });
});
