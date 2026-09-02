"""The six tool hooks (DESIGN §Tool hooks) — the only door a fact or a discount enters a bid by.

R8 is a security property, not a layering preference. The exchange trusts a hosted agent's
claims because the *harness* decided what they say: every value in a hosted bid is minted here,
under a provenance source this module chooses from the evidence it read, and nothing the language
model writes can add to that set. :class:`ToolHooks` is that harness.

Three things make the property checkable rather than merely stated:

* **One minting site.** Every claim is built by :func:`~.provenance.mint_claim`, which stamps the
  source class DESIGN assigns the hook and takes `authority_rank` from the published table. A hook
  cannot mint another hook's source by accident, and `hooks/lint.py` refuses a direct ``Claim(...)``
  anywhere on the hosted path.
* **A ledger of emissions.** Every claim a hook returns is fingerprinted into
  :attr:`ToolHooks.emitted_fingerprints` at the moment it is returned.
  :func:`~.provenance.enforce_hook_provenance` admits a claim if and only if its fingerprint is in
  that ledger — never because the claim's own `provenance.source` says something legitimate.
* **Hard envelope walls.** :meth:`ToolHooks.authorize_discount` is the only way a discount reaches
  an offer, and it refuses anything past the envelope's `max_discount_pct` or under the applicable
  price floor. A refusal is a :class:`Denied` value, not an exception, so a caller has to *look* at
  the answer; there is no `except` clause that quietly proceeds at the requested depth.

Nothing here reads a wall clock, a network, a database or an LLM. Every hook is a pure function of
the store context it was constructed with, which is what makes a hosted bid reproducible
byte-for-byte from its inputs (S4) and what lets the whole suite run offline.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from contracts import Claim, ClaimType, ProvenanceSource

from .provenance import UNKNOWN_OBSERVED_AT, claim_fingerprint, mint_claim, scoped_ref

#: Slack allowed when comparing a requested discount against a wall. Percentages arrive as
#: binary floats; without this a request of exactly `max_discount_pct` could be refused (or a
#: price exactly on the floor accepted) because of a representation error rather than a rule.
#: It is far below a currency unit, so it cannot widen a wall by anything a merchant could feel.
WALL_TOLERANCE = 1e-9

#: Denial reasons `authorize_discount` can return. Stable strings: they are logged, and a
#: merchant-facing explanation is keyed off them.
REASON_UNKNOWN_PRODUCT = "unknown_product"
REASON_NEGATIVE_DISCOUNT = "negative_discount"
REASON_OVER_MAX_DISCOUNT = "over_max_discount_pct"
REASON_BELOW_PRICE_FLOOR = "below_price_floor"

#: The claim-type vocabulary (D53) a hook stamps for the catalog / envelope keys this system
#: actually uses. A key that is not here is minted with `claim_type=None` rather than guessed:
#: the claim_type→trust-dimension mapping is exhaustive, so a wrong type is a wrong trust
#: dimension, which is worse than an absent one.
CLAIM_TYPE_BY_KEY: Mapping[str, ClaimType] = {
    "authorized_discount_pct": ClaimType.discount,
    "compatibility": ClaimType.compatibility,
    "delivery": ClaimType.delivery,
    "dispatch_window": ClaimType.dispatch_window,
    "free_returns": ClaimType.return_policy,
    "ingredients": ClaimType.ingredients,
    "list_price": ClaimType.unit_price,
    "material": ClaimType.specifications,
    "nutrition": ClaimType.nutrition,
    "price": ClaimType.price,
    "promo_eligibility": ClaimType.promo_eligibility,
    "return_policy": ClaimType.return_policy,
    "returns": ClaimType.return_policy,
    "ships_within": ClaimType.dispatch_window,
    "shipping_speed": ClaimType.shipping_speed,
    "specifications": ClaimType.specifications,
    "total_price": ClaimType.total_price,
    "unit_price": ClaimType.unit_price,
    "warranty": ClaimType.warranty,
}

#: The policy version reported when the store has no learned policy yet (R10 cold start).
COLD_START_POLICY_VERSION = "cold-start"


class HookInputError(LookupError):
    """A hook was asked about something the store context does not contain.

    Raised rather than answered with a default: a hook that invented a value for an unknown
    product would be minting a fact from nothing, under a provenance source that says it came
    from the catalog. That is precisely the forgery R8 exists to make impossible.

    `authorize_discount` is the deliberate exception — it *denies* an unknown product instead of
    raising, because a discount hook has a fail-closed answer available and a denial is more
    useful to a bidding runtime than a traceback.
    """


@dataclass(frozen=True)
class Denied:
    """A refusal from :meth:`ToolHooks.authorize_discount`. Falsy, and carries why.

    Deliberately not an exception. A discount request is a normal thing to refuse — the envelope
    walls are hit on ordinary bids — and an exception invites `except Exception: pass` around the
    one call in the system whose refusal must be honoured. Being falsy means the natural
    ``if authorized:`` reads correctly; being a distinct type means a caller that forgets to look
    cannot mistake it for a claim.
    """

    reason: str
    product_ref: str
    requested_pct: float
    limit: float
    rule_ref: str

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return (
            f"discount denied ({self.reason}): {self.requested_pct}% on {self.product_ref!r} "
            f"against limit {self.limit} from {self.rule_ref}"
        )


@dataclass(frozen=True)
class HookCall:
    """One entry in :attr:`ToolHooks.call_log` — which hook ran, on what, and what came back.

    This is the audit trail S5 asks for: "every claim in the bid traces to a hook call" is only
    checkable if the calls were recorded. Values are plain and JSON-shaped so the log can be
    written to `sealed.shadow_bids` verbatim.
    """

    hook: str
    subject: str
    outcome: str
    claim_keys: tuple[str, ...] = ()
    detail: str = ""


def _as_mapping(value: Any, what: str) -> dict[str, Any]:
    """Read a mapping out of a dict or a pydantic model, so fixtures and models behave alike."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise TypeError(f"{what} must be a mapping or a pydantic model, got {type(value).__name__}")


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return [value]
    return list(value)


class ToolHooks:
    """The store-agent's tool harness: six hooks, a ledger, and a call log.

    Constructed from the store context, either positionally (``ToolHooks(context)``) or by
    keyword (``ToolHooks(**context)``). The context is:

    ``store_id``
        the store this agent advocates for; it appears in every provenance `ref`.
    ``envelope``
        the merchant-approved :class:`~contracts.Envelope` (or its dict form). Source of the
        standing commitments, the discount cap and the price floors.
    ``catalog``
        ``{product_ref: {product_ref, list_price, ...attributes}}`` — the scraped catalog.
    ``live_state``
        ``{product_ref: {in_stock, units_left, ...}}`` — the pixel feed. Lossy by design: a
        product with no entry yields no live-state claims rather than an invented one.
    ``learned_policy``
        ``None`` before the store has learned anything (R10), else a mapping carrying
        ``version`` and ``actions: {cluster_id: {...}}``.
    ``network_priors``
        ``{cluster_id: prior}`` — the platform's cross-store prior, discount-blind by
        construction upstream (R17).
    ``as_of``
        optional ISO instant stamped as `observed_at` on evidence that carries none. Explicit
        rather than a clock read, so two runs on identical inputs are byte-identical (S4).
    """

    #: Every claim this facade has emitted, in emission order (S5's audit trail).
    emitted_claims: list[Claim]
    #: Content fingerprints of the same claims — what `enforce_hook_provenance` checks against.
    emitted_fingerprints: set[str]
    #: One :class:`HookCall` per hook invocation, refusals included.
    call_log: list[HookCall]

    def __init__(self, context: Any = None, /, **overrides: Any) -> None:
        merged: dict[str, Any] = {}
        if context is not None:
            merged.update(_as_mapping(context, "store context"))
        merged.update(overrides)

        self.store_id = str(merged.get("store_id") or "")
        self.envelope = _as_mapping(merged.get("envelope"), "envelope")
        self.catalog = {
            str(k): _as_mapping(v, "catalog entry")
            for k, v in _as_mapping(merged.get("catalog"), "catalog").items()
        }
        self.live_state = {
            str(k): _as_mapping(v, "live-state entry")
            for k, v in _as_mapping(merged.get("live_state"), "live_state").items()
        }
        self.learned_policy = merged.get("learned_policy")
        self.network_priors = _as_mapping(merged.get("network_priors"), "network_priors")
        as_of = merged.get("as_of")
        self.as_of = str(as_of) if as_of else None

        self.call_log = []
        self.emitted_claims = []
        self.emitted_fingerprints = set()

    # -- provenance refs -----------------------------------------------------------------

    @property
    def envelope_version(self) -> int:
        raw = self.envelope.get("version", 0)
        return int(raw) if isinstance(raw, (int, float)) else 0

    def envelope_ref(self, path: str) -> str:
        """`envelope:<store>:v<n>#<path>` — the citation form the approved envelope uses."""
        return f"envelope:{self.store_id}:v{self.envelope_version}#{path}"

    def catalog_ref(self, product_ref: str, key: str) -> str:
        return f"catalog:{self.store_id}:{product_ref}#{key}"

    def pixel_ref(self, product_ref: str, key: str) -> str:
        return f"pixel:{self.store_id}:{product_ref}#{key}"

    @property
    def policy_version(self) -> str:
        policy = self.learned_policy
        if isinstance(policy, Mapping):
            version = policy.get("version")
            if version:
                return str(version)
            return "unversioned"
        if policy is None:
            return COLD_START_POLICY_VERSION
        version = getattr(policy, "version", None)
        return str(version) if version else "unversioned"

    # -- the ledger ----------------------------------------------------------------------

    def _emit(self, claims: Iterable[Claim]) -> list[Claim]:
        """Record `claims` as hook emissions and return them.

        Every claim that leaves this facade goes through here. The ledger is what
        `enforce_hook_provenance` checks against, so a claim that skipped this method is
        indistinguishable from one the model wrote — which is the correct outcome.
        """
        recorded = list(claims)
        for claim in recorded:
            self.emitted_claims.append(claim)
            self.emitted_fingerprints.add(claim_fingerprint(claim))
        return recorded

    def _log(
        self,
        hook: str,
        subject: str,
        outcome: str,
        claims: Iterable[Claim] = (),
        detail: str = "",
    ) -> None:
        self.call_log.append(
            HookCall(
                hook=hook,
                subject=subject,
                outcome=outcome,
                claim_keys=tuple(str(c.key) for c in claims),
                detail=detail,
            )
        )

    def _observed_at(self, evidence: Mapping[str, Any] | None = None) -> str:
        """`observed_at` for a claim: the evidence's own stamp, else `as_of`, else the epoch."""
        if evidence:
            stamp = evidence.get("observed_at")
            if stamp:
                return str(stamp)
        return self.as_of or UNKNOWN_OBSERVED_AT

    # -- hook 1: scraped catalog facts ---------------------------------------------------

    def get_product_fact(self, product_ref: str, key: str) -> Claim:
        """One catalog attribute, tagged `scraped`. DESIGN: `get_product_fact -> Claim`."""
        listing = self.catalog.get(str(product_ref))
        if listing is None:
            self._log("get_product_fact", str(product_ref), "unknown_product", detail=key)
            raise HookInputError(
                f"no catalog entry for product_ref {product_ref!r}; known products: "
                f"{sorted(self.catalog)}"
            )
        if key not in listing:
            self._log("get_product_fact", str(product_ref), "unknown_key", detail=key)
            raise HookInputError(
                f"the catalog entry for {product_ref!r} carries no {key!r}; known keys: "
                f"{sorted(listing)}"
            )
        claim = mint_claim(
            key=str(key),
            value=listing[key],
            claim_type=CLAIM_TYPE_BY_KEY.get(str(key)),
            source=ProvenanceSource.scraped,
            ref=self.catalog_ref(str(product_ref), str(key)),
            observed_at=self._observed_at(listing),
        )
        emitted = self._emit([claim])
        self._log("get_product_fact", str(product_ref), "claim", emitted, detail=str(key))
        return emitted[0]

    # -- hook 2: the pixel feed ----------------------------------------------------------

    def get_live_state(self, product_ref: str) -> list[Claim]:
        """Live availability for a product, tagged `pixel_feed`.

        The pixel is lossy by documented design, so a catalog product with no feed entry yields
        an empty list. That is the honest answer: "the pixel has said nothing about this" is not
        the same fact as "it is out of stock", and only one of them is evidence.
        """
        ref = str(product_ref)
        if ref not in self.catalog:
            self._log("get_live_state", ref, "unknown_product")
            raise HookInputError(
                f"no catalog entry for product_ref {ref!r}; the pixel feed is keyed by catalog "
                f"product, known products: {sorted(self.catalog)}"
            )
        state = self.live_state.get(ref) or {}
        observed_at = self._observed_at(state)
        claims = [
            mint_claim(
                key=str(key),
                value=state[key],
                claim_type=CLAIM_TYPE_BY_KEY.get(str(key)),
                source=ProvenanceSource.pixel_feed,
                ref=self.pixel_ref(ref, str(key)),
                observed_at=observed_at,
            )
            for key in sorted(state)
            if key != "observed_at"
        ]
        emitted = self._emit(claims)
        self._log("get_live_state", ref, "claims" if emitted else "no_feed_data", emitted)
        return emitted

    # -- hook 3: the owner's standing commitments ----------------------------------------

    def get_owner_commitments(self, cluster_id: str) -> list[Claim]:
        """The envelope's standing commitments, re-minted `owner_statement`.

        Re-minted rather than passed through: a commitment that arrived on the envelope as a
        plain dict has never been through the ledger, so handing it straight to the bid would
        put a claim in front of `enforce_hook_provenance` that no hook emitted. Minting here is
        what makes the envelope's own words admissible.

        The commitment's `ref`, `observed_at` and `authority_rank` are preserved when the
        envelope carries them — the envelope is the evidence, and a hook may legitimately
        down-rank (D30) but must not silently promote or re-date a merchant's statement.
        """
        commitments = _sequence(self.envelope.get("standing_commitments"))
        claims: list[Claim] = []
        for index, raw in enumerate(commitments):
            entry = _as_mapping(raw, "standing commitment")
            key = str(entry.get("key") or "")
            if not key:
                raise HookInputError(
                    f"standing commitment #{index} on envelope {self.store_id!r} has no `key`, "
                    "so it cannot be cited as evidence"
                )
            provenance = _as_mapping(entry.get("provenance"), "commitment provenance")
            rank = provenance.get("authority_rank")
            claims.append(
                mint_claim(
                    key=key,
                    value=entry.get("value"),
                    unit=entry.get("unit"),
                    claim_type=entry.get("claim_type") or CLAIM_TYPE_BY_KEY.get(key),
                    source=ProvenanceSource.owner_statement,
                    ref=str(provenance.get("ref") or self.envelope_ref(key)),
                    observed_at=self._observed_at(provenance),
                    authority_rank=int(rank) if isinstance(rank, (int, float)) else None,
                )
            )
        emitted = self._emit(claims)
        self._log("get_owner_commitments", str(cluster_id), "claims", emitted)
        return emitted

    # -- hook 4: the envelope walls ------------------------------------------------------

    def price_floor(self, product_ref: str) -> float:
        """The binding floor for a product: the stricter of its own floor and the store-wide one.

        An `EnvelopeFloor` with no `product_ref` is the store-wide rule, so both can apply to the
        same product; taking the maximum means adding a store-wide floor can only ever tighten,
        never loosen, a per-product one.
        """
        floor = 0.0
        for raw in _sequence(self.envelope.get("floors")):
            entry = _as_mapping(raw, "envelope floor")
            scope = entry.get("product_ref")
            if scope in (None, "", str(product_ref)):
                floor = max(floor, float(entry.get("min_price") or 0.0))
        return floor

    def authorize_discount(self, product_ref: str, requested_pct: float) -> Claim | Denied:
        """Authorize a discount against the envelope, or refuse. `envelope_rule` provenance.

        The two walls are independent and both hard:

        * `max_discount_pct` caps the depth outright — a request past it is refused whatever the
          price works out to;
        * the applicable price floor caps the resulting *price* — a request comfortably inside
          the cap is still refused if it takes the item under the floor.

        A request exactly at the cap, or landing exactly on the floor, is authorized: the
        envelope states a limit the merchant approved, not a limit to stay below.

        The granted claim cites **both** walls it cleared: `#max_discount_pct@<product_ref>`, the
        rule and the product it was evaluated against. That is not decoration. The floors are per
        product — `prod-cap` clears 20% off 100.00 against a 10.00 floor while `prod-floor`
        refuses the identical request against a 95.00 one — so a grant that cited only the
        store-wide rule would be the same claim in both worlds, carry the same fingerprint into
        :attr:`emitted_fingerprints`, and let the refused product spend the other's grant. Naming
        the product is what makes an authorization non-transferable; the matching wall lives in
        :func:`~.provenance.enforce_hook_provenance`.
        """
        ref = str(product_ref)
        try:
            pct = float(requested_pct)
        except (TypeError, ValueError) as exc:
            raise HookInputError(f"requested discount {requested_pct!r} is not a number") from exc

        cap = float(self.envelope.get("max_discount_pct") or 0.0)
        listing = self.catalog.get(ref)
        if listing is None:
            return self._deny(ref, pct, REASON_UNKNOWN_PRODUCT, 0.0, self.envelope_ref("floors"))
        if pct < 0.0:
            return self._deny(
                ref, pct, REASON_NEGATIVE_DISCOUNT, 0.0, self.envelope_ref("max_discount_pct")
            )
        if pct > cap + WALL_TOLERANCE:
            return self._deny(
                ref, pct, REASON_OVER_MAX_DISCOUNT, cap, self.envelope_ref("max_discount_pct")
            )

        list_price = float(listing.get("list_price") or 0.0)
        # `list_price * (100 - pct) / 100` rather than `list_price * (1 - pct/100)`: the first
        # keeps whole percentages exact (100 * 97 / 100 == 97.0), the second does not.
        resulting_price = list_price * (100.0 - pct) / 100.0
        floor = self.price_floor(ref)
        if resulting_price + WALL_TOLERANCE < floor:
            return self._deny(
                ref, pct, REASON_BELOW_PRICE_FLOOR, floor, self.envelope_ref(f"floors#{ref}")
            )

        claim = mint_claim(
            key="authorized_discount_pct",
            value=pct,
            unit="percent",
            claim_type=ClaimType.discount,
            source=ProvenanceSource.envelope_rule,
            ref=scoped_ref(self.envelope_ref("max_discount_pct"), ref),
            observed_at=self._observed_at(),
        )
        emitted = self._emit([claim])
        self._log(
            "authorize_discount",
            ref,
            "authorized",
            emitted,
            detail=f"{pct}% -> {resulting_price} (floor {floor}, cap {cap})",
        )
        return emitted[0]

    def _deny(
        self, product_ref: str, pct: float, reason: str, limit: float, rule_ref: str
    ) -> Denied:
        denial = Denied(
            reason=reason,
            product_ref=product_ref,
            requested_pct=pct,
            limit=limit,
            rule_ref=rule_ref,
        )
        self._log("authorize_discount", product_ref, reason, detail=f"{pct}% vs limit {limit}")
        return denial

    # -- hook 5: the learned policy ------------------------------------------------------

    def choose_policy_action(self, context: Any) -> Claim:
        """The action the store's own learned policy selects, tagged `learned_policy`.

        The returned claim's `value` IS the action — `{policy_version, cluster_id, product_ref,
        discount_pct, commitment_keys}` — so a runtime cannot act on a policy decision that is
        not itself provenance-tagged evidence, and the policy version is logged with every call.

        With no learned policy (R10 cold start) the action is the deterministic default: zero
        discount, the envelope's standing commitments, and version ``cold-start``. There is no
        improvisation path.
        """
        ask = _as_mapping(context, "policy context")
        cluster_id = str(ask.get("cluster_id") or "")
        product_ref = str(ask.get("product_ref") or "")
        version = self.policy_version

        commitment_keys = tuple(
            str(_as_mapping(c, "standing commitment").get("key") or "")
            for c in _sequence(self.envelope.get("standing_commitments"))
        )
        action: dict[str, Any] = {
            "policy_version": version,
            "cluster_id": cluster_id,
            "product_ref": product_ref,
            "discount_pct": 0.0,
            "commitment_keys": sorted(k for k in commitment_keys if k),
        }
        learned = self.learned_policy
        if isinstance(learned, Mapping):
            by_cluster = _as_mapping(learned.get("actions"), "learned policy actions")
            chosen = _as_mapping(by_cluster.get(cluster_id), "learned policy action")
            for name in ("discount_pct", "commitment_keys", "value_prop"):
                if name in chosen:
                    action[name] = chosen[name]

        claim = mint_claim(
            key="policy_action",
            value=action,
            source=ProvenanceSource.learned_policy,
            ref=f"policy:{self.store_id}:{version}#{cluster_id}",
            observed_at=self._observed_at(),
        )
        emitted = self._emit([claim])
        self._log("choose_policy_action", cluster_id, "action", emitted, detail=version)
        return emitted[0]

    # -- hook 6: the network prior -------------------------------------------------------

    def get_network_prior(self, cluster_id: str) -> Claim:
        """The platform's cross-store prior for a cluster, tagged `network`.

        A cluster the network has no prior for returns a claim whose value is the empty prior,
        cited at a `#absent` ref. "The network has nothing to say here" is itself a network
        observation, and a cold cluster must be distinguishable from a rich one in the bid's
        evidence rather than silently absent from it.
        """
        cluster = str(cluster_id)
        prior = self.network_priors.get(cluster)
        present = prior is not None
        evidence = prior if isinstance(prior, Mapping) else None
        claim = mint_claim(
            key="network_prior",
            value=prior if present else {},
            source=ProvenanceSource.network,
            ref=f"network-prior:{cluster}" if present else f"network-prior:{cluster}#absent",
            observed_at=self._observed_at(evidence),
        )
        emitted = self._emit([claim])
        self._log("get_network_prior", cluster, "prior" if present else "absent", emitted)
        return emitted[0]


__all__ = [
    "CLAIM_TYPE_BY_KEY",
    "COLD_START_POLICY_VERSION",
    "REASON_BELOW_PRICE_FLOOR",
    "REASON_NEGATIVE_DISCOUNT",
    "REASON_OVER_MAX_DISCOUNT",
    "REASON_UNKNOWN_PRODUCT",
    "WALL_TOLERANCE",
    "Denied",
    "HookCall",
    "HookInputError",
    "ToolHooks",
]
