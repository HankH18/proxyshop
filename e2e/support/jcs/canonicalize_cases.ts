/**
 * Drive the TypeScript canonicalizer over a case file written by the Python conformance gate.
 *
 * `e2e/test_jcs_conformance.py` owns every decision about what is a case and what the answer
 * should be; this file is deliberately dumb. It rebuilds inputs, calls `canonicalJson`, and
 * reports back what happened — including *that* a call threw, which is half of what the gate
 * checks.
 *
 * Protocol — newline-delimited JSON, one case per line:
 *
 *   $JCS_CASE_FILE   `{"id": "...", "input": <encoded value>}` per line
 *   stdout           `{"id", "ok", "out", "error", "echo"}` per line
 *
 * The path arrives in the environment rather than in `argv` because `vite-node` inserts its own
 * arguments: under it `argv[2]` is this file's own path, so an `argv[2]` protocol silently reads
 * the driver's source as its case file and dies in `JSON.parse` on the leading comment.
 *
 * Why inputs are ENCODED rather than sent as plain JSON
 * -----------------------------------------------------
 * Every string — object keys included — travels as an array of UTF-16 code units, and is rebuilt
 * here with `String.fromCharCode`. Nothing in the transport depends on `JSON.parse` decoding a
 * `\uXXXX` escape, and that is not defensive programming: measured on Node v25.9.0, feeding this
 * corpus to `JSON.parse` produced an object whose key was U+005C (`\`) where the document plainly
 * spelled `""`. It reproduces with plain `node`, no Vite involved, and it survives splitting
 * the document into one `JSON.parse` per line — so it is state carried across parse calls, not a
 * property of any one document, and no amount of chunking escapes it. The gate would have blamed
 * a canonicalizer for a difference in what the two languages were even asked to canonicalize.
 *
 * The encoding is a tagged tuple, so every JSON type survives exactly:
 *
 *   ["z"]                             null
 *   ["b", true|false]                 boolean
 *   ["n", <number>]                   number (JSON numbers are unaffected by the fault above)
 *   ["s", [<code unit>, ...]]         string
 *   ["a", [<encoded>, ...]]           array
 *   ["o", [[[<code unit>, ...], <encoded>], ...]]   object, in the sender's member order
 *
 * Code units rather than code points, so a lone surrogate — which RFC 8785 §3.2.2.2 requires the
 * canonicalizer to REFUSE, and which therefore has to arrive intact to be refused — is carried
 * as literally the unpaired unit it is.
 *
 * Run it with `vite-node`, not bare `node`: `signing.ts` imports `"./schemas.js"` — the
 * TypeScript-source spelling of a `.ts` file — and Node's own type stripping does not resolve
 * that (`ERR_MODULE_NOT_FOUND` on `schemas.js`), while Vite's resolver does.
 */
import {readFileSync} from "node:fs";
import {env, stdout} from "node:process";

import {canonicalJson} from "../../../packages/contracts/src/ts/signing.ts";

interface Result {
  id: string;
  ok: boolean;
  out: string;
  error: string;
  echo: string;
}

function fromCodeUnits(units: number[]): string {
  // Built one unit at a time rather than with a spread: `String.fromCharCode(...units)` passes
  // every unit as an argument and overflows the stack on a long string.
  let text = "";
  for (const unit of units) text += String.fromCharCode(unit);
  return text;
}

function decode(node: unknown): unknown {
  const [tag, payload] = node as [string, unknown];
  switch (tag) {
    case "z":
      return null;
    case "b":
      return payload as boolean;
    case "n":
      return payload as number;
    case "s":
      return fromCodeUnits(payload as number[]);
    case "a":
      return (payload as unknown[]).map(decode);
    case "o": {
      const out: Record<string, unknown> = {};
      for (const [key, value] of payload as [number[], unknown][]) {
        // `defineProperty`, not `out[key] = …`: a member literally named `__proto__` would go
        // through the prototype setter and vanish from the object instead of becoming a key.
        Object.defineProperty(out, fromCodeUnits(key), {
          value: decode(value),
          enumerable: true,
          writable: true,
          configurable: true,
        });
      }
      return out;
    }
    default:
      throw new Error(`unknown encoding tag ${JSON.stringify(tag)}`);
  }
}

const casePath = env.JCS_CASE_FILE;
if (casePath === undefined || casePath === "") {
  throw new Error("JCS_CASE_FILE is unset; e2e/test_jcs_conformance.py sets it");
}

const results: Result[] = [];
for (const line of readFileSync(casePath, "utf8").split("\n")) {
  if (line === "") continue;
  const entry = JSON.parse(line) as {id: string; input: unknown};
  const input = decode(entry.input);
  // Re-serialized before anything else touches it: the Python side compares this against its own
  // input and refuses to grade any case whose value did not survive the trip.
  const echo = JSON.stringify(input);
  try {
    results.push({id: entry.id, ok: true, out: canonicalJson(input), error: "", echo});
  } catch (error) {
    // The gate compares *whether* a value was refused, never which class was thrown: the two
    // Python implementations define their own unrelated `CanonicalisationError`s and this one
    // is a third. The name travels only so a failure message can say what happened.
    const name = error instanceof Error ? error.name : typeof error;
    results.push({id: entry.id, ok: false, out: "", error: name, echo});
  }
}

stdout.write(results.map((result) => JSON.stringify(result)).join("\n"));
