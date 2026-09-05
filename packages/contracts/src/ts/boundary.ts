/**
 * The dual-path bid boundary (R8 / R18 / S5), in TypeScript.
 *
 * Deliberately a line-for-line peer of `contracts/boundary.py`: same reason codes, same ordering,
 * same fail-closed rules. A boundary that admitted different bids depending on which language the
 * caller happened to be written in would not be a boundary. `tests/boundary.test.ts` checks the
 * TypeScript verdicts against the same table the Python tests use.
 *
 * **A claim is judged by where it CAME FROM, never by which field it was written in.** All three
 * claim-bearing sites are walked — `bid.claims`, `offer.commitments` and `offer.discount` — since
 * the Offer is inside the bid boundary. A boundary that inspected only `bid.claims` did not have
 * an exclusivity property; it had a naming convention, and moving the claim one field over
 * defeated it outright.
 *
 * **And a price is judged by the depth it declares, not by the grant sitting beside it.** T-177:
 * a bid carrying a genuine hook-minted 20% grant, declaring 20% and charging 15.00 on a 100.00
 * list price was admitted here with `ok=true, reasons=[]`, because the only wall comparing what a
 * bid CHARGES with what it DECLARES lived on the emitting side. See `priceReasons`.
 */
import type {Bid, BidValidationResult, LedgerEvent} from "../../generated/ts/protocol.schema.d.ts";
import {validationErrors} from "./schemas.js";
import {missingSigningFields} from "./signing.js";

export type BidPathName = "hosted" | "external";

export const HOSTED_PATH = "hosted" as const;
export const EXTERNAL_PATH = "external" as const;
export const BID_PATHS: readonly BidPathName[] = [HOSTED_PATH, EXTERNAL_PATH];

/**
 * R8: the six provenance sources a store-agent can only mint by calling a tool hook (T-040 pins the
 * hook→source table). Anything else in a hosted bid means the hooks were bypassed.
 */
export const HOOK_PROVENANCE_SOURCES: ReadonlySet<string> = new Set([
  "scraped",
  "pixel_feed",
  "owner_statement",
  "envelope_rule",
  "learned_policy",
  "network",
]);

/** The only source no hook produces — an assertion the seller made in free text. */
export const NON_HOOK_PROVENANCE_SOURCES: ReadonlySet<string> = new Set(["seller_asserted"]);

/**
 * The claim-bearing sites inside a `Bid` that are NOT `bid.claims`, spelled the way the reason
 * strings name them. `Claim` and `Discount` are the only objects in the protocol schema carrying a
 * `provenance` block, and the only ones reachable from a `Bid` are `bid.claims[i]`,
 * `bid.offer.commitments[i]` and `bid.offer.discount` — `Bid` forbids extra keys, so that list is
 * closed. Add a site to the schema and it must be added here, or R8 stops covering it.
 */
export const OFFER_COMMITMENTS_SITE = "offer.commitments";
export const OFFER_DISCOUNT_SITE = "offer.discount";

/** The two priced sites on an offer, spelled the way the price reasons name them. */
export const OFFER_UNIT_PRICE_SITE = "offer.unit_price";
export const OFFER_TOTAL_PRICE_SITE = "offer.total_price";

/**
 * `Discount.type` spellings that mean "`value` is a percentage depth" — the only form this
 * boundary can reconcile. An amount off cannot be compared with a stated price without
 * re-deriving what the offer means, and a boundary that re-derived prices would be deciding
 * rather than checking. The same three spellings the store-agent's emitting wall accepts.
 */
export const PERCENTAGE_DISCOUNT_TYPES: ReadonlySet<string> = new Set([
  "percentage",
  "percent",
  "pct",
]);

/**
 * The claim key under which a bid carries the list price its discount is a percentage OF.
 * `get_product_fact(product_ref, "list_price")` is the hook that mints it, so a hosted bid can
 * only carry a list price the catalog actually published. It is the only list price the boundary
 * can see when the caller supplies no roster — it holds no catalog of its own, and inventing a
 * lookup it cannot perform would be worse than saying so. Since T-306 a caller supplying no roster
 * is refused by `list_price_unavailable` anyway, so this claim no longer decides such a bid on its
 * own; it still decides the contradiction check against a roster that IS readable. It is also the
 * SAME key a `listPrices` roster row may spell its number under, so one name means one thing on
 * both sides.
 */
export const LIST_PRICE_CLAIM_KEY = "list_price";

/**
 * The `listPrices` roster row key naming the deepest percentage discount the caller AUTHORIZES for
 * a product. Deliberately the policy `Envelope`'s own spelling — `max_discount_pct`, the field
 * `authorize_discount` refuses to mint past on the emitting side. There is NO claim by this name:
 * a cap a bid could carry would be a cap the bid chooses. The Python peer's
 * `MAX_DISCOUNT_ROSTER_KEY`.
 */
export const MAX_DISCOUNT_ROSTER_KEY = "max_discount_pct";

/**
 * Slack when reconciling a stated price against the price its declared depth prices out at, as
 * an absolute amount of currency rather than a float epsilon. Money is quoted to the cent, so an
 * honest rounded price sits a fraction of a cent under the exact one and a wall tightened to the
 * float would refuse almost every real product. Identical to the Python peer's constant.
 */
export const PRICE_RECONCILIATION_TOLERANCE = 0.01;

export const REASON_UNKNOWN_PATH = "unknown_path";
export const REASON_SCHEMA_INVALID = "schema_invalid";
export const REASON_CLAIM_WITHOUT_PROVENANCE = "claim_without_provenance";
export const REASON_CLAIM_PROVENANCE_EMPTY_SOURCE = "claim_provenance_empty_source";
export const REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE = "claim_provenance_unknown_source";
export const REASON_HOSTED_NON_HOOK_PROVENANCE = "hosted_non_hook_provenance";
/**
 * An assertable source at a claim-bearing site the verification queue has no address for.
 * `unverified_claim_indexes` names positions in `bid.claims`; a `seller_asserted` claim living in
 * `offer.commitments` or on `offer.discount` cannot be pointed at through it, so the external
 * path refuses it here rather than admitting something nobody will ever verify.
 */
export const REASON_UNVERIFIABLE_CLAIM_SITE = "unverifiable_claim_site";
export const REASON_OFFER_EXPIRED = "offer_expired";
export const REASON_OFFER_EXPIRY_MISSING = "offer_expiry_missing";
export const REASON_OFFER_EXPIRY_UNPARSEABLE = "offer_expiry_unparseable";
export const REASON_STORE_BLACKLISTED = "store_blacklisted";
export const REASON_TRUST_SNAPSHOT_UNAVAILABLE = "trust_snapshot_unavailable";
export const REASON_SIGNING_ENVELOPE_INCOMPLETE = "signing_envelope_incomplete";
export const REASON_SIGNATURE_MISSING = "signature_missing";
/**
 * The offer charges less than the depth it declares licenses. One-sided: charging MORE than the
 * depth prices out at spends less of the envelope than was declared, which is a question about
 * what the buyer is told, not about what the merchant authorised.
 */
export const REASON_PRICE_UNDER_DECLARED_DEPTH = "price_under_declared_depth";
/**
 * The depth, the price, or the list price is a number this boundary cannot read, so the offer's
 * arithmetic cannot be checked at all. Fail-closed, exactly like an unavailable eligibility read.
 */
export const REASON_PRICE_UNRECONCILABLE = "price_unreconcilable";
/**
 * The offer declares a depth deeper than the caller authorizes for that product. Distinct from
 * `price_under_declared_depth` on purpose: that one says the offer is priced under its own
 * paperwork, this one says the paperwork itself was never granted, and only one of the two is
 * fixable by repricing.
 */
export const REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH = "discount_over_authorized_depth";

/**
 * `price_unreconcilable` suffixes for the caller-supplied roster, named rather than spelled inline
 * so a caller can match on them. A roster is EVIDENCE like any other, so every way of failing to
 * read one is its own refusal — never a silent downgrade back to the claim-only abstention, which
 * would hand an attacker one unknown `product_ref` as a way round the entire wall. The Python
 * peer's `ROSTER_*` constants, string for string.
 */
export const ROSTER_LIST_PRICE_UNAVAILABLE = "list_price_unavailable";
export const ROSTER_LIST_PRICE_UNREADABLE = "unreadable_roster_list_price";
export const ROSTER_LIST_PRICE_CONTRADICTED = "list_price_contradicts_roster";
export const ROSTER_MAX_DISCOUNT_UNAVAILABLE = "authorized_depth_unavailable";
export const ROSTER_MAX_DISCOUNT_UNREADABLE = "unreadable_authorized_depth";
/**
 * The roster prices this product ABOVE zero and the offer charges nothing for it. Alone among the
 * suffixes here it says nothing about reading the roster: it is the one refusal in the price walk
 * that no declared depth and no authorized cap can talk its way out of. See the floor in
 * `priceReasonsFor`.
 */
export const ROSTER_PRICE_NOT_POSITIVE = "not_positive";

/**
 * The same floor one keystroke over: the offer's price clears zero and nothing else. Kept apart
 * from `ROSTER_PRICE_NOT_POSITIVE` deliberately, because they say different things to the operator
 * reading a rejection: `not_positive` is "this is not a number a price can be" and this is "this is
 * a number, and it is not enough of one". A price that is zero, negative or unreadable keeps this
 * boundary's own name for that and never also collects this one — one bad number reported under
 * two names is a mislabelling, not a second finding. Identical to the Python peer's constant.
 */
export const ROSTER_PRICE_BELOW_FLOOR = "below_price_floor";

/**
 * The smallest amount of money a price is allowed to name — one minor unit of the currency the
 * exchange prices in. A number under this is not a cheap price, it is a price nobody can pay: no
 * settlement rail moves 0.001, and 1e-09 is nine orders of magnitude under the smallest coin there
 * is. It is the ABSOLUTE half of `priceFloor` and it is what makes the floor bite on a cheap
 * product, where a percentage of the list price is itself a fraction of a cent. Identical to the
 * Python peer's constant.
 */
export const MINIMUM_PAYABLE_AMOUNT = 0.01;

/**
 * ...and the PROPORTIONAL half, as a fraction of the roster row's own list price, which is what
 * makes the floor bite on an expensive one: 0.02 on a product listed at 1,000,000.00 is a payable
 * amount and still not a price for it.
 *
 * Why 0.1% and not something stricter: the frozen R10 contract pins how far this may go. The
 * exchange's `test_r10_still_admits_an_undeclared_undercut_where_nothing_is_authorized` asserts
 * that 99.00, 80.00 and 1.00 are all admitted on a row listing at 100.00 with no authorized depth,
 * so any floor at or above 1.00 on that row breaks a standing assertion — the ceiling on this
 * constant is therefore 1%, and 0.1% takes it with an order of magnitude of margin while still
 * refusing 0.001 and 1e-09. Identical to the Python peer's constant.
 */
export const PRICE_FLOOR_FRACTION = 0.001;

/**
 * The lowest number that is still a price for a product the roster lists at `listed`.
 *
 * Both halves at once — the larger of the proportional floor and one minor currency unit — because
 * each covers the case the other misses. On a 100.00 product the proportional half dominates
 * (0.10); on a 0.50 product the absolute half does (0.01, where 0.1% would be five thousandths of
 * a cent and would refuse nothing). The absolute half is dropped, not clamped, where the roster
 * itself lists the product below one minor unit: a catalog genuinely pricing something at 0.005 is
 * not describing a giveaway, and a floor above its own list price would refuse every bid on it —
 * closed rather than fail-closed.
 *
 * The line-for-line peer of `price_floor` in `contracts/boundary.py`. The two doors must round the
 * same way or the shared price-parity corpus reports a divergence, which is exactly what it is for.
 */
export function priceFloor(listed: number): number {
  const proportional = listed * PRICE_FLOOR_FRACTION;
  return listed >= MINIMUM_PAYABLE_AMOUNT
    ? Math.max(proportional, MINIMUM_PAYABLE_AMOUNT)
    : proportional;
}

export interface TrustSnapshotRow {
  store_id?: string;
  score?: number;
  blacklisted?: boolean;
}

export type TrustSnapshotMap = Record<string, TrustSnapshotRow | undefined>;

export interface ValidateBidOptions {
  path: string;
  trustSnapshot: TrustSnapshotMap;
  /** The instant expiry is judged against. Defaults to now; pass it to stay deterministic. */
  now?: Date | string | number;
  /**
   * When true, D52's five envelope fields and a non-empty `signature` are required, each gap
   * reported as its own reason. Off by default: `Bid` carries no envelope, and a hosted Tier-1
   * agent holds no key. `validateExternalSubmission` is this flag turned on.
   */
  requireSigningEnvelope?: boolean;
  /**
   * The caller's OWN catalog, `{product_ref: 100.0}` or
   * `{product_ref: {list_price: 100.0, max_discount_pct: 20.0}}`. Pass it and the price wall stops
   * depending on the bid volunteering what it is discounting from — the one move that defeated it,
   * and the only one an emitter did not have to forge anything to make.
   *
   * **A roster you pass is evidence, and so is the roster you do not** (T-306/T-336). Omitting
   * this argument, passing `null` and passing `{}` are ONE call: a product the roster cannot price
   * is `list_price_unavailable` however the roster came to be empty. Omission used to abstain,
   * which made the argument a caller FORGETS more permissive than the argument a caller passes
   * empty, on the money path — so a caller holding no catalog is now told it cannot price what it
   * admits, exactly as a caller passing an empty one is.
   */
  listPrices?: PriceRosterMap;
  /**
   * A caller-wide ceiling in percentage points, for a caller holding one approved number rather
   * than a per-product column. A roster row's own `max_discount_pct` beats it. Read whether or not
   * a roster is passed (T-307): it used to be reached only through `listPrices`, so setting it to
   * 0 — "I authorize no discount" — while omitting the roster silently authorized everything.
   */
  maxDiscountPct?: number;
}

/** A roster row: the number itself, or a record spelling it under `list_price`. */
export type PriceRosterRow = number | Record<string, unknown> | null | undefined;
export type PriceRosterMap = Record<string, PriceRosterRow>;

/** Options for the standalone {@link priceReasons}, for a caller holding no trust snapshot. */
export interface PriceReasonOptions {
  listPrices?: PriceRosterMap;
  maxDiscountPct?: number;
}

function readRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

/**
 * Read `key` off `table` only when the table OWNS it.
 *
 * `table[key]` is not a lookup when the key comes off the wire; it is a walk up the prototype
 * chain. A seller who registered the store_id `__proto__` got `Object.prototype` back — an
 * object, non-null, not an array, so `readRecord` accepted it — and the
 * `trust_snapshot_unavailable` refusal never fired, while `row["blacklisted"]` was `undefined`
 * and therefore falsy. That bid was ADMITTED with no trust row behind it at all. `constructor`
 * and `toString` behaved the same way in kind. The Python peer never had this: a dict lookup of
 * `"__proto__"` is a miss like any other, so this was also a live ok-divergence between the two
 * doors, on the eligibility gate specifically.
 */
function readOwn(table: Record<string, unknown>, key: string): unknown {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

/**
 * `value` as a number when it IS one, else `undefined`. No coercion: `"20"` is a string a seller
 * wrote, not a depth. The Python peer's `_finite_number` reads the same set of values.
 */
function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/**
 * Parse an RFC-3339 instant, a `Z`-suffixed instant, or epoch seconds. `undefined` if unreadable.
 * An offset-less value is read as UTC: a boundary whose expiry depended on the host's timezone
 * would not be a contract.
 */
export function parseTimestamp(value: unknown): Date | undefined {
  if (value === null || value === undefined) return undefined;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? undefined : value;
  if (typeof value === "number" && Number.isFinite(value)) return new Date(value * 1000);
  if (typeof value !== "string" || value.trim() === "") return undefined;
  let text = value.trim();
  const hasZone = /(?:Z|z|[+-]\d{2}:?\d{2})$/.test(text);
  if (!hasZone) text = `${text}Z`;
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed;
}

/**
 * Judge ONE provenance-bearing object. Returns its reasons and whether it needs verifying.
 *
 * `holder` is anything carrying a `provenance` block — a `Claim` from `bid.claims` or from
 * `offer.commitments`, or the `Discount` on the offer. Same table for all three: R8 is a rule
 * about where a statement came from, and it does not become a different rule because the
 * statement was written in a different field.
 *
 * `label` is how the reason names the site: the bare integer index for `bid.claims` (that
 * spelling is published — `hosted_non_hook_provenance:1:seller_asserted` is asserted verbatim by
 * both language suites), and a dotted/bracketed path for every other site.
 *
 * `addressable` is R18's admit-and-flag, available at `bid.claims` ONLY — see `offerClaimReasons`.
 */
function sourceVerdict(
  holder: unknown,
  path: BidPathName,
  label: string,
  addressable: boolean,
): {reasons: string[]; needsVerification: boolean} {
  const record = readRecord(holder);
  const provenance = readRecord(record?.["provenance"]);
  if (record === undefined || record["provenance"] === null || record["provenance"] === undefined) {
    return {reasons: [`${REASON_CLAIM_WITHOUT_PROVENANCE}:${label}`], needsVerification: false};
  }
  const source = String(provenance?.["source"] ?? "").trim();
  if (source === "") {
    return {reasons: [`${REASON_CLAIM_PROVENANCE_EMPTY_SOURCE}:${label}`], needsVerification: false};
  }
  if (HOOK_PROVENANCE_SOURCES.has(source)) return {reasons: [], needsVerification: false};
  if (NON_HOOK_PROVENANCE_SOURCES.has(source)) {
    if (path === HOSTED_PATH) {
      // R8/S5: a hosted agent cannot mint this source through any hook.
      return {
        reasons: [`${REASON_HOSTED_NON_HOOK_PROVENANCE}:${label}:${source}`],
        needsVerification: false,
      };
    }
    // R18: admitted, but it goes to verification before it is shown as fact — and only where
    // the verification queue has an address for it.
    if (addressable) return {reasons: [], needsVerification: true};
    return {
      reasons: [`${REASON_UNVERIFIABLE_CLAIM_SITE}:${label}:${source}`],
      needsVerification: false,
    };
  }
  return {
    reasons: [`${REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE}:${label}:${source}`],
    needsVerification: false,
  };
}

/**
 * Per-claim provenance verdicts over one list of claims.
 *
 * `site` is `undefined` for `bid.claims` — the published, addressable list — and the dotted path
 * of the containing field for any other list. Only the `bid.claims` walk returns indexes, because
 * `unverified_claim_indexes` means positions in `bid.claims` and nothing else.
 */
function claimProvenanceReasons(
  claims: unknown,
  path: BidPathName,
  site?: string,
): {reasons: string[]; unverified: number[]} {
  const reasons: string[] = [];
  const unverified: number[] = [];
  if (claims === null || claims === undefined) return {reasons, unverified};
  if (!Array.isArray(claims)) return {reasons: [REASON_SCHEMA_INVALID], unverified};

  claims.forEach((claim, index) => {
    const label = site === undefined ? String(index) : `${site}[${index}]`;
    const verdict = sourceVerdict(claim, path, label, site === undefined);
    reasons.push(...verdict.reasons);
    if (verdict.needsVerification) unverified.push(index);
  });

  return {reasons, unverified};
}

/**
 * R8 at the OTHER claim-bearing sites: `offer.commitments` and `offer.discount`.
 *
 * **The offer is inside the bid boundary.** `claimProvenanceReasons` used to be handed
 * `record["claims"]` and nothing else, so the whole exclusivity property was defeated by MOVING
 * the payload: the identical `seller_asserted` claim, relocated into `offer.commitments`, turned
 * a rejection into `ok=true, reasons=[]` — with an unauthorised 25% discount riding along. Same
 * claim, same source, same bid; only the field moved. These are the only two other sites:
 * `Claim` and `Discount` are the sole objects in the protocol schema carrying a `provenance`
 * block, and `Bid` forbids extra keys.
 *
 * A non-hook source here is REFUSED on both paths, not flagged. R18 does not mean "admitted", it
 * means "admitted *and routed to verification*", and the only handle the boundary gives the queue
 * is `unverified_claim_indexes` — a published `number[]` addressing `bid.claims`. Renumbering it
 * to cover offer sites would point the queue at the wrong claims; admitting a claim it has no
 * address for would be a worse hole than the one this walk closes. An external agent may still
 * assert freely — in `bid.claims`, the channel that has an address.
 */
function offerClaimReasons(offer: unknown, path: BidPathName): string[] {
  const record = readRecord(offer);
  if (record === undefined) return [];

  const reasons = claimProvenanceReasons(
    record["commitments"],
    path,
    OFFER_COMMITMENTS_SITE,
  ).reasons;

  // Judged only when there IS a discount — `discount` is nullable by schema. But a discount that
  // is PRESENT and carries no provenance is refused like any other unprovenanced statement: it is
  // the field that actually moves money, and leaving "no provenance at all" unjudged would reopen
  // this exploit one step further down — drop the block instead of relabelling it, and the 25%
  // discount walks again.
  const discount = record["discount"];
  if (discount !== null && discount !== undefined) {
    reasons.push(...sourceVerdict(discount, path, OFFER_DISCOUNT_SITE, false).reasons);
  }

  return reasons;
}

/**
 * The percentage depth THIS offer declares, and why it could not be read. Mirrors
 * `_declared_depth` in `boundary.py` case for case.
 */
function declaredDepth(offer: Record<string, unknown>): {
  depth: number | undefined;
  reasons: string[];
} {
  const discount = offer["discount"];
  if (discount === null || discount === undefined) return {depth: 0, reasons: []};

  const record = readRecord(discount);
  const depth = finiteNumber(record?.["value"]);
  if (depth === undefined) {
    return {
      depth: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${OFFER_DISCOUNT_SITE}:depth_not_a_number`],
    };
  }
  if (depth === 0) return {depth: 0, reasons: []};

  const kind = record?.["type"];
  // The raw spelling reaches the reason string, the way a provenance source does — but only when
  // it IS a string. `String(2.0)` is "2" here and "2.0" in Python, and a reason code that differs
  // between the two doors is a parity break dressed up as a diagnostic.
  const spelling = typeof kind === "string" ? kind.trim().toLowerCase() : "not_a_string";
  if (!PERCENTAGE_DISCOUNT_TYPES.has(spelling)) {
    return {
      depth: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${OFFER_DISCOUNT_SITE}:${spelling}`],
    };
  }
  if (depth < 0 || depth > 100) {
    return {
      depth: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${OFFER_DISCOUNT_SITE}:depth_out_of_range`],
    };
  }
  return {depth, reasons: []};
}

/**
 * The list price the BID carries, from any `list_price` claim at any claim-bearing site. Mirrors
 * `_carried_list_price` in `boundary.py`.
 *
 * Two claims naming two different list prices is refused rather than resolved: an `Offer` names
 * exactly one `product_ref`, so exactly one list price is coherent, and picking one of them would
 * let a bid choose which wall it is measured against by adding a claim.
 */
function carriedListPrice(
  bid: Record<string, unknown>,
  offer: Record<string, unknown>,
): {listed: number | undefined; reasons: string[]} {
  const values: number[] = [];
  let unreadable = false;

  for (const claims of [bid["claims"], offer["commitments"]]) {
    if (!Array.isArray(claims)) continue;
    for (const claim of claims) {
      const key = readRecord(claim)?.["key"];
      if (typeof key !== "string" || key.trim() !== LIST_PRICE_CLAIM_KEY) continue;
      const listed = finiteNumber(readRecord(claim)?.["value"]);
      // A claim that says "here is the list price" and then does not state a number is not an
      // absent list price; it is an unreadable one, and reading past it would let a bid disable
      // this wall by making its own evidence illegible.
      if (listed === undefined || listed < 0) unreadable = true;
      else values.push(listed);
    }
  }

  if (unreadable) {
    return {
      listed: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${OFFER_UNIT_PRICE_SITE}:unreadable_list_price`],
    };
  }
  const distinct = [...new Set(values)].sort((a, b) => a - b);
  if (distinct.length > 1) {
    return {
      listed: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${OFFER_UNIT_PRICE_SITE}:ambiguous_list_price`],
    };
  }
  return {listed: distinct[0], reasons: []};
}

/** "the roster has no row for this product", distinct from a row that IS `null`. */
const NO_ROW = Symbol("no roster row");

/**
 * The roster's row for this offer's product, or `NO_ROW`. Mirrors `_roster_row` in `boundary.py`.
 *
 * `readOwn` and not `roster[ref]`: `product_ref` comes off the wire, so a bid naming the product
 * `__proto__` would otherwise walk the prototype chain and be handed an object — the same hole
 * `readOwn` exists to close on the trust snapshot, on the one lookup that decides a price.
 */
function rosterRow(offer: Record<string, unknown>, listPrices: PriceRosterMap | undefined): unknown {
  // `readRecord` alone, with no `=== undefined` arm in front of it: it already answers `undefined`
  // for `undefined`, for `null`, for an array and for a primitive, so the roster nobody passed,
  // the roster spelled `null` and the roster that is not a readable object are ONE case.
  //
  // THIS PARTICULAR EDIT CHANGED NO BEHAVIOUR and the comment here used to claim otherwise. The
  // arm that sat in front — `listPrices === undefined ? undefined : readRecord(listPrices)` —
  // was REDUNDANT, not wrong: `readRecord(undefined)` is already `undefined`. Measured by
  // reverting this line alone, the whole vitest suite stays green — and that is the CORRECT
  // result, not a coverage gap: `typeof undefined` is `"undefined"`, never `"object"`, so
  // `readRecord(undefined)` is already `undefined` and the removed ternary was a tautology on
  // every input. A red here would have meant a test asserting something false. T-336's actual divergence lived in the two `if (listPrices
  // === undefined) return <abstain>` guards in `rosterListPrice` and `authorizedDepth`: those
  // fired for `undefined` and NOT for `null`, so an explicit `null` was an empty roster on this
  // door and an absent one on the Python peer, which tested `is None`. Removing those closed it.
  const table = readRecord(listPrices);
  if (table === undefined) return NO_ROW;
  const productRef = offer["product_ref"];
  if (typeof productRef !== "string" && typeof productRef !== "number") return NO_ROW;
  const row = readOwn(table, String(productRef));
  return row === null || row === undefined ? NO_ROW : row;
}

/**
 * The list price the EXCHANGE holds for this offer's product. Mirrors `_roster_list_price`.
 *
 * **There is no abstention for an ABSENT roster** (T-306/T-336). This used to open
 * `if (listPrices === undefined) return {listed: undefined, reasons: []}` — the abstention "only
 * when the caller passed no roster at all" — and that sentence was the fail-open: it made
 * FORGETTING the argument more permissive than passing it empty, on the one door where the
 * difference is money. Every way of failing to read a roster is a refusal, and the roster nobody
 * passed is now read exactly like `{}`: a product it does not price is `list_price_unavailable`,
 * and one it prices with something that is not a non-negative finite number is
 * `unreadable_roster_list_price`. Neither degrades back to silence.
 */
function rosterListPrice(
  offer: Record<string, unknown>,
  listPrices: PriceRosterMap | undefined,
): {listed: number | undefined; reasons: string[]} {
  const site = OFFER_UNIT_PRICE_SITE;
  const row = rosterRow(offer, listPrices);
  if (row === NO_ROW) {
    return {
      listed: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${site}:${ROSTER_LIST_PRICE_UNAVAILABLE}`],
    };
  }

  const direct = finiteNumber(row);
  const listed = direct ?? finiteNumber(readRecord(row)?.[LIST_PRICE_CLAIM_KEY]);
  if (listed === undefined || listed < 0) {
    return {
      listed: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${site}:${ROSTER_LIST_PRICE_UNREADABLE}`],
    };
  }
  return {listed, reasons: []};
}

/**
 * The deepest percentage the CALLER authorizes for this product. Mirrors `_authorized_depth`.
 *
 * The half of T-177 that makes the other half bind: `priceReasons` bounds the stated price by the
 * depth the offer DECLARES, and a bound chosen by the thing being bounded is not a bound. The cap
 * is read from the caller — the roster row's own `max_discount_pct` first, then the call's
 * `maxDiscountPct` — and never from the bid. Nothing is a REFUSAL, not a default.
 *
 * **There is no abstention for an ABSENT roster, and removing that carve-out is T-306/T-307.**
 * This used to open `if (listPrices === undefined) return {cap: undefined, reasons: []}`, which
 * sat BEFORE the cap is read at all — so a caller passing `maxDiscountPct: 0` ("I authorize no
 * discount") and forgetting the roster had its ceiling dropped on the floor with nothing in the
 * verdict saying so. The two arguments together ARE the price wall, and omitting one disarmed
 * both rather than failing closed on the missing input.
 */
function authorizedDepth(
  offer: Record<string, unknown>,
  listPrices: PriceRosterMap | undefined,
  maxDiscountPct: number | undefined,
): {cap: number | undefined; reasons: string[]} {
  const site = OFFER_DISCOUNT_SITE;
  const row = rosterRow(offer, listPrices);
  const fromRow = row === NO_ROW ? undefined : readRecord(row)?.[MAX_DISCOUNT_ROSTER_KEY];
  const raw = fromRow === undefined || fromRow === null ? maxDiscountPct : fromRow;
  if (raw === undefined || raw === null) {
    return {
      cap: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${site}:${ROSTER_MAX_DISCOUNT_UNAVAILABLE}`],
    };
  }

  const cap = finiteNumber(raw);
  if (cap === undefined || cap < 0 || cap > 100) {
    return {
      cap: undefined,
      reasons: [`${REASON_PRICE_UNRECONCILABLE}:${site}:${ROSTER_MAX_DISCOUNT_UNREADABLE}`],
    };
  }
  return {cap, reasons: []};
}

/**
 * Reconcile the price the offer STATES against the depth the caller AUTHORIZES (T-177). The
 * line-for-line peer of `_price_reasons` in `boundary.py` — see that docstring for why both
 * relations exist, why they are one-sided, and what the first one deliberately cannot know.
 */
function priceReasonsFor(
  bid: Record<string, unknown>,
  offer: unknown,
  listPrices?: PriceRosterMap,
  maxDiscountPct?: number,
): string[] {
  const record = readRecord(offer);
  if (record === undefined) return [];

  const reasons: string[] = [];
  const declared = declaredDepth(record);
  reasons.push(...declared.reasons);

  const unitPrice = finiteNumber(record["unit_price"]);
  const totalPrice = finiteNumber(record["total_price"]);
  if (unitPrice === undefined) {
    reasons.push(`${REASON_PRICE_UNRECONCILABLE}:${OFFER_UNIT_PRICE_SITE}:not_a_number`);
  } else if (unitPrice < 0) {
    reasons.push(`${REASON_PRICE_UNRECONCILABLE}:${OFFER_UNIT_PRICE_SITE}:negative`);
  }
  if (totalPrice === undefined) {
    reasons.push(`${REASON_PRICE_UNRECONCILABLE}:${OFFER_TOTAL_PRICE_SITE}:not_a_number`);
  } else if (totalPrice < 0) {
    reasons.push(`${REASON_PRICE_UNRECONCILABLE}:${OFFER_TOTAL_PRICE_SITE}:negative`);
  }

  const depth = declared.depth;
  // Nothing arithmetic left to say: the numbers the relations are built out of are already
  // refused above, and restating them as an inequality would report one bad number twice.
  if (depth === undefined || unitPrice === undefined || unitPrice < 0) return reasons;

  // The depth the offer declared is paperwork; the depth the CALLER authorized is the bound. A
  // zero depth needs no authorization — it takes nothing off — so the cap is not even consulted
  // for one.
  //
  // T-337's third site, and the one a two-site repair leaves alive: `authorized = listPrices ===
  // undefined ? depth : 0` restored the abstention one layer up by handing the offer its own
  // declared depth back as the bound, which is the self-granting cap `authorizedDepth` exists to
  // end. There is no `listPrices === undefined` branch left in this walk.
  let authorized = depth;
  if (depth > 0) {
    const granted = authorizedDepth(record, listPrices, maxDiscountPct);
    reasons.push(...granted.reasons);
    if (granted.cap === undefined) {
      // A roster that could not authorize this depth, and an unauthorized depth authorizes
      // nothing. An ABSENT roster is one of those rosters now (T-306), not an exemption.
      authorized = 0;
    } else {
      // No tolerance on this comparison, and none is wanted: a request exactly AT the cap is
      // authorized (`authorize_discount` grants at `max_discount_pct` and denies above it), and a
      // cent of currency slack has no meaning applied to percentage points.
      if (depth > granted.cap) {
        reasons.push(`${REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH}:${OFFER_DISCOUNT_SITE}`);
      }
      authorized = Math.min(depth, granted.cap);
    }
  } else {
    // NO depth declared, and a roster to check against — and after T-306 there is ALWAYS a roster
    // to check against, because an absent one is an empty one. The price is still a depth — an implicit
    // one — and 15.00 for a 100.00 product is an 85% discount however the paperwork is spelled. So
    // when the roster STATES what is authorized, the undeclared price is measured against that,
    // exactly as a declared one is. The cap's own reasons are dropped: `authorized_depth_
    // unavailable` names a depth the offer claimed without authorization, and this offer claimed
    // nothing. `authorized` then stays at zero, so an absent or unreadable cap still measures the
    // price against the full list price — the fail-closed direction.
    const granted = authorizedDepth(record, listPrices, maxDiscountPct);
    if (granted.cap !== undefined) authorized = granted.cap;
  }

  const carried = carriedListPrice(bid, record);
  reasons.push(...carried.reasons);
  const rostered = rosterListPrice(record, listPrices);
  reasons.push(...rostered.reasons);

  // The roster wins when both are readable, and a disagreement between them is its own refusal
  // rather than a tie broken silently: letting the door pick would let a bid choose which wall it
  // is measured against by writing a claim.
  const listed = rostered.listed ?? carried.listed;
  if (
    rostered.listed !== undefined &&
    carried.listed !== undefined &&
    Math.abs(rostered.listed - carried.listed) > PRICE_RECONCILIATION_TOLERANCE
  ) {
    reasons.push(
      `${REASON_PRICE_UNRECONCILABLE}:${OFFER_UNIT_PRICE_SITE}:${ROSTER_LIST_PRICE_CONTRADICTED}`,
    );
  }

  // THE FLOOR — the one relation here that no depth can satisfy. Everything else is an inequality
  // against `authorized`, so an authorized 100 makes all of them true at once: `listed * (100 -
  // 100) / 100` is 0.00, and a bid charging 0.00 for a 100.00 product is then arithmetically
  // perfect. Measured through the exchange's own `POST /auctions` before this existed — roster row
  // `{list_price: 100.0, max_discount_pct: 100.0}`, an offer declaring 100% at 0.00 — `HTTP 201,
  // entries=[{fallback: false, unit_price: 0.0}]`. A price of nothing is not a deep discount; it is
  // the absence of a price, and no authorization makes a product free.
  //
  // Deliberately `rostered`, never `listed`: a carried claim must not switch this on, or the
  // emitter would choose its own floor and every no-roster verdict would stop being identical.
  //
  // And it is a THRESHOLD, not an equality. It was written `priced === 0`, which has exactly one
  // satisfying value while every other relation here is an inequality against a caller-supplied
  // depth — so the floor was one keystroke wide, and the answer to it was to write `0.001` instead
  // of `0`. Measured on the Python peer at HEAD, roster `{'prod-1': 100.0}` with a cap of 100:
  // `priceReasons(bid(0.001, depth=100))` and `priceReasons(bid(1e-09))` both came back `[]` —
  // this boundary saying it has no objection to a 99.999% undercut on a 100.00 product. See
  // `priceFloor` for the number and for why it cannot be tighter.
  //
  // The two refusals are kept apart and never both reported for one price: `not_positive` is "this
  // is not a number a price can be" (exactly zero — a negative price is already `:negative` above)
  // and `below_price_floor` is "this is a number, and it is not enough of one".
  if (rostered.listed !== undefined && rostered.listed > 0) {
    const floor = priceFloor(rostered.listed);
    for (const [site, priced] of [
      [OFFER_UNIT_PRICE_SITE, unitPrice],
      [OFFER_TOTAL_PRICE_SITE, totalPrice],
    ] as const) {
      if (priced === undefined) {
        continue;
      }
      if (priced === 0) {
        reasons.push(`${REASON_PRICE_UNRECONCILABLE}:${site}:${ROSTER_PRICE_NOT_POSITIVE}`);
      } else if (priced > 0 && priced < floor) {
        reasons.push(`${REASON_PRICE_UNRECONCILABLE}:${site}:${ROSTER_PRICE_BELOW_FLOOR}`);
      }
    }
  }

  if (
    listed !== undefined &&
    unitPrice + PRICE_RECONCILIATION_TOLERANCE < (listed * (100 - authorized)) / 100
  ) {
    reasons.push(`${REASON_PRICE_UNDER_DECLARED_DEPTH}:${OFFER_UNIT_PRICE_SITE}`);
  }

  // An authorized depth above zero only. At zero there is no depth to make true, and `total >=
  // unit` would no longer be a statement about a discount — it would conflate quantity with
  // discount. The list-price relation above still applies at zero: a price under LIST with no
  // authorized discount is a discount that entered the bid through no hook at all.
  if (
    authorized > 0 &&
    totalPrice !== undefined &&
    totalPrice >= 0 &&
    totalPrice + PRICE_RECONCILIATION_TOLERANCE < (unitPrice * (100 - authorized)) / 100
  ) {
    reasons.push(`${REASON_PRICE_UNDER_DECLARED_DEPTH}:${OFFER_TOTAL_PRICE_SITE}`);
  }

  return reasons;
}

/**
 * The PRICE half of `validateBid`'s verdict, for a caller holding a catalog but no trust snapshot.
 *
 * The peer of `price_reasons` in `boundary.py`, and the SAME function `validateBid` calls rather
 * than a second copy of the arithmetic. A caller that already ran the R12 eligibility gate
 * elsewhere has no snapshot to hand this door and would otherwise have to invent a permissive one
 * to reach the price walk. Never throws.
 */
export function priceReasons(bid: unknown, options: PriceReasonOptions = {}): string[] {
  const record = readRecord(bid) ?? {};
  return priceReasonsFor(record, record["offer"], options.listPrices, options.maxDiscountPct);
}

function expiryReasons(offer: unknown, now: Date): string[] {
  const record = readRecord(offer);
  if (record === undefined) return [REASON_OFFER_EXPIRY_MISSING];
  const raw = record["expires_at"];
  if (raw === null || raw === undefined || (typeof raw === "string" && raw.trim() === "")) {
    // Fail closed. An offer with no stated expiry is an offer nobody can price the risk of.
    return [REASON_OFFER_EXPIRY_MISSING];
  }
  const expiresAt = parseTimestamp(raw);
  if (expiresAt === undefined) return [REASON_OFFER_EXPIRY_UNPARSEABLE];
  return expiresAt.getTime() <= now.getTime() ? [REASON_OFFER_EXPIRED] : [];
}

function eligibilityReasons(storeId: unknown, snapshot: TrustSnapshotMap): string[] {
  const table = readRecord(snapshot);
  if (table === undefined) return [REASON_TRUST_SNAPSHOT_UNAVAILABLE];
  const key = String(storeId ?? "");
  // `readOwn`, never `table[key]`: the key is caller-supplied, and a bare index walks the
  // prototype chain. `store_id: "__proto__"` returned `Object.prototype` — an object that
  // `readRecord` accepts and that has no `blacklisted` — so the store was admitted with no trust
  // row at all. Own properties only; everything inherited is an unavailable read.
  const row = readRecord(readOwn(table, key));
  // R12, fail-closed: blacklisted denies, and an unavailable read denies the same way.
  if (row === undefined) return [`${REASON_TRUST_SNAPSHOT_UNAVAILABLE}:${key}`];
  // TRUTHY, not `=== true`. A strict comparison admits a store whose row spells the flag `1` or
  // `"yes"` — the fail-OPEN direction, on the one check R12 exists to make fail closed. The
  // Python peer reads it the same way, and `boundary.test.ts` pins both spellings.
  if (row["blacklisted"]) return [`${REASON_STORE_BLACKLISTED}:${key}`];
  return [];
}

/**
 * D52: the five envelope fields plus a `signature`, or the submission is refused.
 *
 * Read off the raw submission, not off a validated `Bid`: `Bid` carries no envelope by design, so
 * a schema check against it can never see these fields.
 */
function signingEnvelopeReasons(bid: unknown): string[] {
  const record = readRecord(bid) ?? {};
  const reasons = missingSigningFields(record).map(
    (field) => `${REASON_SIGNING_ENVELOPE_INCOMPLETE}:${field}`,
  );
  const signature = record["signature"];
  if (typeof signature !== "string" || signature.trim() === "") {
    reasons.push(REASON_SIGNATURE_MISSING);
  }
  return reasons;
}

/**
 * Decide whether `bid` may enter the auction through `options.path`. Never throws.
 *
 * Does NOT check the signing envelope unless `options.requireSigningEnvelope` is set — see
 * `validateExternalSubmission`, which is the wire door.
 */
export function validateBid(bid: unknown, options: ValidateBidOptions): BidValidationResult {
  const evaluatedAt = parseTimestamp(options.now) ?? new Date();

  if (options.path !== HOSTED_PATH && options.path !== EXTERNAL_PATH) {
    return {
      ok: false,
      path: String(options.path),
      reasons: [`${REASON_UNKNOWN_PATH}:${String(options.path)}`],
      requires_verification: false,
      unverified_claim_indexes: [],
    };
  }
  const path: BidPathName = options.path;
  const reasons: string[] = [];

  // 1. Schema validity — refused on every path, before any field is interpreted. The model
  //    depends on what the caller says it is holding: `Bid` forbids extra keys, so validating a
  //    real D52 submission against it would report the four envelope fields as schema
  //    violations — the mirror image of admitting a bid that has none of them.
  const schemaProblems = validationErrors(
    options.requireSigningEnvelope ? "SignedBidSubmission" : "Bid",
    bid,
  );
  if (schemaProblems.length > 0) {
    reasons.push(`${REASON_SCHEMA_INVALID}:${schemaProblems.slice(0, 8).join(";")}`);
  }

  const record = readRecord(bid) ?? {};

  // 2. Provenance, at EVERY claim-bearing site. Path-sensitive: this is the whole of R8/R18.
  //    `bid.claims` is not the only place a claim can be written down — the Offer is inside the
  //    bid boundary and carries `commitments` and a provenance-stamped `discount` — and a walk
  //    that covers one site is not an exclusivity property, it is a naming convention.
  const claims = claimProvenanceReasons(record["claims"], path);
  reasons.push(...claims.reasons);
  reasons.push(...offerClaimReasons(record["offer"], path));

  // 3. The PRICE the offer states, against the depth it declares. Path-insensitive: arithmetic
  //    does not care who is speaking. A genuine hook-minted 20% grant used to license any price
  //    at all here, because the only wall comparing what a bid charges with what it declares
  //    lived on the emitting side, where a store not running our runtime never meets it. The list
  //    price and the authorized depth it reconciles against come from the bid's own claim and,
  //    when the caller supplies one, from the caller's roster — the version of this wall silence
  //    cannot disarm, and the one the bid does not get to choose its own cap for.
  reasons.push(
    ...priceReasonsFor(record, record["offer"], options.listPrices, options.maxDiscountPct),
  );

  // 4. Offer expiry and 5. seller eligibility — path-insensitive.
  reasons.push(...expiryReasons(record["offer"], evaluatedAt));
  reasons.push(...eligibilityReasons(record["store_id"], options.trustSnapshot));

  // 6. D52's signing envelope, when the caller is the external door rather than a component
  //    judging an already-extracted `Bid`.
  if (options.requireSigningEnvelope) reasons.push(...signingEnvelopeReasons(bid));

  const ok = reasons.length === 0;
  return {
    ok,
    path,
    reasons,
    // A rejected bid is not "admitted pending verification": there is nothing to verify.
    requires_verification: ok && claims.unverified.length > 0,
    unverified_claim_indexes: ok ? claims.unverified : [],
  };
}

/**
 * The Tier-2 door: everything `validateBid` judges, plus D52's signing envelope.
 *
 * The function an exchange should call on a body arriving at
 * `POST /v1/auctions/{auction_id}/bids`. It refuses an unsigned submission with the same finality
 * as an expired offer or a blacklisted store, and, like `validateBid`, it never throws.
 */
export function validateExternalSubmission(
  submission: unknown,
  options: Omit<ValidateBidOptions, "path" | "requireSigningEnvelope">,
): BidValidationResult {
  return validateBid(submission, {
    ...options,
    path: EXTERNAL_PATH,
    requireSigningEnvelope: true,
  });
}

export type {Bid, BidValidationResult, LedgerEvent};
