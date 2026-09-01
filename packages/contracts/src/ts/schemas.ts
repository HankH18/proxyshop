/**
 * Runtime access to the JSON Schema bundle from TypeScript.
 *
 * The generated `.d.ts` gives compile-time types; this gives run-time validation, which is the
 * half that matters at a boundary where the data arrives from somewhere else. Both are produced
 * from `packages/contracts/schemas/protocol.schema.json`, so a TypeScript consumer and a Python
 * consumer accept and reject exactly the same payloads.
 *
 * Ajv is compiled ONCE per schema name and cached: compiling a 46-definition bundle per call
 * would make validation cost more than the work it guards.
 */
// The draft 2020-12 build, NOT the default `ajv` entry point. The default export is the draft-07
// Ajv, which does not know `https://json-schema.org/draft/2020-12/schema` and fails every compile
// with "no schema with key or ref" — a failure that looks like a broken schema rather than a
// wrong import.
import Ajv2020, {type ErrorObject, type ValidateFunction} from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

import bundle from "../../schemas/protocol.schema.json" with {type: "json"};

export const PROTOCOL_SCHEMA_ID = "https://proxyshop.dev/schemas/protocol.schema.json";

export type JsonObject = Record<string, unknown>;

/** The parsed schema bundle, exactly as committed. */
export const protocolSchema = bundle as unknown as {
  $schema: string;
  $id: string;
  $defs: Record<string, JsonObject>;
};

/** Every protocol object the bundle defines, sorted. */
export function schemaNames(): string[] {
  return Object.keys(protocolSchema.$defs).sort();
}

/**
 * A SELF-CONTAINED JSON Schema for one protocol object: a `$ref` plus the whole `$defs` table.
 * Self-contained so a caller needs no resolver and no registry — the same object works here, in
 * Python's `jsonschema`, and pasted into any tool.
 *
 * It deliberately carries NO `$id`. Every subschema would otherwise be a distinct registration
 * under the same bundle id, and a second validator instance compiling the same object would
 * collide with the first.
 */
export function schemaFor(name: string): JsonObject {
  if (!Object.prototype.hasOwnProperty.call(protocolSchema.$defs, name)) {
    throw new Error(
      `${name} is not a protocol object; the bundle defines ${schemaNames().join(", ")}`,
    );
  }
  return {
    $schema: protocolSchema.$schema,
    $ref: `#/$defs/${name}`,
    $defs: protocolSchema.$defs,
  };
}

const ajv = addFormats(
  new Ajv2020({allErrors: true, strict: false, allowUnionTypes: true}),
);

const compiled = new Map<string, ValidateFunction>();

function validatorFor(name: string): ValidateFunction {
  const cached = compiled.get(name);
  if (cached) return cached;
  const fn = ajv.compile(schemaFor(name));
  compiled.set(name, fn);
  return fn;
}

function describe(error: ErrorObject): string {
  const where = error.instancePath === "" ? "<root>" : error.instancePath.replace(/^\//, "").replaceAll("/", ".");
  return `${where}: ${error.message ?? "is invalid"}`;
}

/** Every validation problem for `instance`. An empty array means it is that object. */
export function validationErrors(name: string, instance: unknown): string[] {
  const validate = validatorFor(name);
  if (validate(instance)) return [];
  return (validate.errors ?? []).map(describe);
}

/** True when `instance` validates against the named protocol schema. */
export function isValid(name: string, instance: unknown): boolean {
  return validatorFor(name)(instance);
}

/** Throw with every problem listed when `instance` is not the named object. */
export function assertValid(name: string, instance: unknown): void {
  const errors = validationErrors(name, instance);
  if (errors.length > 0) {
    throw new Error(`${name} is invalid:\n  ${errors.join("\n  ")}`);
  }
}
