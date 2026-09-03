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

import math
from collections import Counter
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
#: NaN, +inf, -inf. A wall is a pair of comparisons, and *every* comparison against NaN is
#: False — so `pct < 0` and `pct > cap` both said "fine" and a NaN depth walked through both
#: walls unchallenged. It failed later, in the canonical-JSON serializer, as a
#: `CanonicalisationError` no caller is told to expect; a wall must refuse it, not a serializer.
REASON_NON_FINITE_DISCOUNT = "non_finite_discount"
#: The catalog lists the product but names no number this hook can read as its price — text, a
#: nested object, a missing key, NaN, inf. Distinct from `REASON_UNKNOWN_PRODUCT`, which is "the
#: catalog has never heard of it": here the merchant does sell the thing and the entry describing
#: it is unusable, which is an operator's problem to see rather than a bidder's to route around.
#: Every discount is a percentage *of* the list price, so with no readable list price there is no
#: arithmetic to run and nothing to authorize — see :meth:`ToolHooks._evaluate_discount`.
REASON_UNPRICEABLE_PRODUCT = "unpriceable_product"

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


class _Sealed(list):  # type: ignore[type-arg]
    """A list that reads like a list and cannot be written through.

    The audit trail is handed out as one of these. S5's criterion is "every claim in the bid
    traces to a hook call", which is only checkable if the record of the calls is a record: a
    party that can append to `call_log` or `emitted_claims` can write its own history, and the
    party holding the facade is precisely the one the harness exists to constrain.

    A `list` subclass rather than a `tuple` because the audit trail is compared against lists and
    indexed like one all over the suite, and rather than a plain copy because a copy would drop a
    write *silently* — a maintainer who appends here should hear about it, not find out later
    that the record is short.
    """

    __slots__ = ()

    def _sealed(self, *_args: Any, **_kwargs: Any) -> Any:
        raise TypeError(
            "the hook audit trail is read-only: it records what the hooks actually did, and a "
            "caller that could add to it could write its own history (S5)"
        )

    append = extend = insert = remove = pop = clear = sort = reverse = _sealed
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _sealed


class _AdmissionLedger:
    """What the hooks emitted for the current bid, and which of it has been spent.

    A separate object, held privately, because of what R8 actually argues: the ledger cannot be
    written by the thing being guarded. The thing being guarded is the hosted path, and the
    hosted path *holds the facade* — that is what a tool harness is for. A public mutable set on
    :class:`ToolHooks` therefore hands the guarded party a pen: one ``hooks.emitted_fingerprints
    .add(...)`` and a claim no hook ever emitted is admissible, with every other wall in this
    package still standing and still useless. So the set is reachable only from here, the facade
    exposes immutable snapshots, and the only mutations offered are the two the harness needs —
    recording an emission, and spending an authorization.

    Neither of those is a hole. Recording happens inside :meth:`ToolHooks._emit`, which is the
    definition of "a hook emitted this". Spending can only ever *reduce* what is admissible, so
    a caller that abuses it refuses its own bid.

    Emissions are **counted, not merely noted**, and that is a decision rather than an
    implementation detail. A fingerprint is content-addressed, so two `authorize_discount` calls
    for the same depth on the same product produce the same fingerprint — deliberately, because a
    hosted bid must be byte-identical across two runs (S4) and a nonce would end that. If the
    ledger were a plain set, "one grant is spendable once" would silently become "this *depth* is
    spendable once per bid", and calling the hook a second time would buy nothing. Counting keeps
    the sentence true as written: N authorizations, N spends.
    """

    __slots__ = ("_emitted", "_spent")

    def __init__(self) -> None:
        self._emitted: Counter[str] = Counter()
        self._spent: Counter[str] = Counter()

    def record(self, fingerprint: str) -> None:
        self._emitted[fingerprint] += 1

    def spend(self, fingerprint: str) -> bool:
        """Redeem one emission. False when every emission of it has already been redeemed."""
        if self._spent[fingerprint] >= self._emitted[fingerprint]:
            return False
        self._spent[fingerprint] += 1
        return True

    def remaining(self, fingerprint: str) -> int:
        """How many emissions of `fingerprint` are still unspent."""
        return max(0, self._emitted[fingerprint] - self._spent[fingerprint])

    @property
    def emitted(self) -> frozenset[str]:
        return frozenset(self._emitted)

    @property
    def spent(self) -> frozenset[str]:
        """Fingerprints with no unspent emission left — what the boundary refuses as spent."""
        return frozenset(
            fingerprint
            for fingerprint, count in self._emitted.items()
            if self._spent[fingerprint] >= count
        )


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

    #: The bid currently open, as passed to :meth:`start_bid`. Empty before the first one.
    bid_ref: str

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

        self.__calls: list[HookCall] = []
        self.__claims: list[Claim] = []
        self.__ledger = _AdmissionLedger()
        self.bid_ref = ""

    @property
    def emitted_claims(self) -> list[Claim]:
        """Every claim this facade has emitted, in emission order, across every bid.

        S5's audit trail, handed out sealed — see :class:`_Sealed`. Never reset, even by
        :meth:`start_bid`: "what did this facade ever emit, and when" is the question an audit
        asks, and scoping admission is not licence to forget.
        """
        return _Sealed(self.__claims)

    @property
    def call_log(self) -> list[HookCall]:
        """One :class:`HookCall` per hook invocation, refusals included. Sealed, never reset."""
        return _Sealed(self.__calls)

    @property
    def emitted_fingerprints(self) -> frozenset[str]:
        """Content fingerprints of what was emitted **for the current bid**.

        What `enforce_hook_provenance` checks against, handed out as a frozen snapshot: the live
        set is private, so the party this ledger guards cannot enter its own forgeries into it.
        Reset by :meth:`start_bid`, so a grant is spendable in the bid it was granted for rather
        than for the life of the object.
        """
        return self.__ledger.emitted

    @property
    def spent_fingerprints(self) -> frozenset[str]:
        """The authorizations already redeemed in this bid. See :meth:`spend_authorization`."""
        return self.__ledger.spent

    def spend_authorization(self, fingerprint: str) -> bool:
        """Redeem an emitted authorization, exactly once. True if this call was the redemption.

        Called by :func:`~.provenance.enforce_hook_provenance` when it admits a grant. A grant is
        one authorization — the walls were checked once, for one product, at one moment — and a
        boundary that only asked "is it in the ledger" would let a single `authorize_discount`
        call furnish the discount for every offer in the bid.

        Public because the boundary lives in another module and must be able to call it, and
        harmless for being public: spending can only ever shrink what is admissible.
        """
        return self.__ledger.spend(str(fingerprint))

    def remaining_authorizations(self, fingerprint: str) -> int:
        """How many unspent authorizations of `fingerprint` this bid still holds.

        Asked *before* anything is spent, because "exactly once" has to hold inside a single
        boundary call as well as across two: a bid that lists one grant three times must be
        refused, and a membership test of what has already been spent cannot see that — nothing
        has been spent yet. The boundary counts what a call asks for and compares it with this.
        """
        return self.__ledger.remaining(str(fingerprint))

    def start_bid(self, bid_ref: str = "") -> None:
        """Open a new bid: nothing emitted for the previous one stays admissible.

        :attr:`emitted_fingerprints` is an admission ledger, and without this it is also
        unbounded in time — a grant obtained once would stay redeemable for as long as the facade
        lives, so one `authorize_discount` call could furnish a discount to every later auction.
        S5 asks that every claim in *this* bid trace to a hook call, which is a statement about
        one bid; a ledger that spans all of them cannot answer it.

        The audit trail is deliberately NOT reset. :attr:`emitted_claims` and :attr:`call_log`
        keep growing across bids, because "what did this facade ever emit, and when" is the
        question an audit asks. Scoping admission is not licence to forget.

        **The contract, stated so it is a decision rather than an omission.** Constructing a
        facade opens its first bid — a fresh :class:`ToolHooks` starts with an empty ledger — so
        a runtime that builds one per auction is already correct and needs none of this. A facade
        REUSED across auctions must call this between them; there is no way for the harness to
        detect a new auction on its own, and a grant is spendable until it is told. The safe
        pattern is therefore "one facade per bid, or `start_bid()` per bid", and the unsafe one
        is a long-lived facade that never says when a bid ended.
        """
        self.bid_ref = str(bid_ref)
        self.__ledger = _AdmissionLedger()

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
            # A deep copy: the audit trail must be a record of what the hooks did, and the
            # object the hook returned belongs to the caller, who can edit it afterwards.
            self.__claims.append(claim.model_copy(deep=True))
            self.__ledger.record(claim_fingerprint(claim))
        return recorded

    def _log(
        self,
        hook: str,
        subject: str,
        outcome: str,
        claims: Iterable[Claim] = (),
        detail: str = "",
    ) -> None:
        self.__calls.append(
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

    def list_price(self, product_ref: str) -> float | None:
        """The catalog's undiscounted price for a product, or `None` when there is no usable one.

        The number every discount is a percentage *of*. It was already read here — inside
        :meth:`_evaluate_discount`, to work out what a requested depth prices out at — but only
        ever for the hook's own decision, so the bid boundary had no way to ask what 20% off was
        supposed to come to and could only check the price against the floor. Exposed as a method
        beside :meth:`price_floor` for the same reason that one is: the boundary and the hook must
        read the merchant's numbers from one place, or they will eventually disagree about them.

        `None` rather than `0.0` for a product the catalog does not list, or one whose entry
        carries no usable `list_price`. The distinction matters at the boundary: 0.0 is a list
        price that makes every stated price look generous, while `None` is "unanswerable", and
        :func:`~.provenance._price_reconciliation_refusal` refuses rather than guesses when it is
        handed one.
        """
        listing = self.catalog.get(str(product_ref))
        if listing is None:
            return None
        try:
            value = float(listing.get("list_price"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

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

        denial, resulting_price, floor, cap = self._evaluate_discount(ref, pct)
        if denial is not None:
            reason, limit, rule_ref = denial
            return self._deny(ref, pct, reason, limit, rule_ref)

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

    def _evaluate_discount(
        self, product_ref: str, pct: float
    ) -> tuple[tuple[str, float, str] | None, float, float, float]:
        """The envelope's verdict on a depth: ``(denial | None, price, floor, cap)``. Pure.

        Split out of :meth:`authorize_discount` so the walls can be re-asked without minting a
        second claim or writing a second call-log entry. One arithmetic, two callers — the hook
        that grants, and :meth:`would_authorize`, which the bid boundary uses to re-check a grant
        it is being handed. Two copies of this arithmetic would be two chances to disagree about
        what the merchant approved.

        **A list price nobody can read is a denial, not a zero.** This read the catalog directly
        and `float("one hundred dollars")` raised, which was ugly but was at least impossible to
        miss. Routing it through :meth:`list_price` — correct, so the boundary and the hook read
        one number — was written as ``self.list_price(ref) or 0.0``, and that `or` put the
        fail-open back: the accessor answers `None` for "unanswerable", `None or 0.0` is a real
        list price of 0.0, every depth then prices out at 0.0, and 0.0 clears any floor the
        envelope does not name for that product. A malformed catalog entry stopped raising and
        started minting genuine, ledger-recorded grants — inside the method whose whole job is to
        refuse things. So the `None` is now its own denial (`REASON_UNPRICEABLE_PRODUCT`): loud,
        named, logged, and fail-closed, without reintroducing the crash the accessor prevents at
        the bid boundary. A catalog that genuinely lists 0.00 is *answerable* and still priced —
        which is the distinction `or` erased, since 0.0 is falsy and `None` is too.
        """
        cap = float(self.envelope.get("max_discount_pct") or 0.0)
        listing = self.catalog.get(product_ref)
        if listing is None:
            return (REASON_UNKNOWN_PRODUCT, 0.0, self.envelope_ref("floors")), 0.0, 0.0, cap
        if not math.isfinite(pct):
            # Before the comparisons, because NaN answers False to all of them: `pct < 0.0` and
            # `pct > cap` would both pass and the depth would be "authorized" by two walls that
            # never actually compared anything.
            rule = self.envelope_ref("max_discount_pct")
            return (REASON_NON_FINITE_DISCOUNT, cap, rule), 0.0, 0.0, cap
        if pct < 0.0:
            rule = self.envelope_ref("max_discount_pct")
            return (REASON_NEGATIVE_DISCOUNT, 0.0, rule), 0.0, 0.0, cap
        if pct > cap + WALL_TOLERANCE:
            rule = self.envelope_ref("max_discount_pct")
            return (REASON_OVER_MAX_DISCOUNT, cap, rule), 0.0, 0.0, cap

        list_price = self.list_price(product_ref)
        if list_price is None:
            rule = self.envelope_ref(f"catalog#{product_ref}")
            return (REASON_UNPRICEABLE_PRODUCT, 0.0, rule), 0.0, 0.0, cap
        # `list_price * (100 - pct) / 100` rather than `list_price * (1 - pct/100)`: the first
        # keeps whole percentages exact (100 * 97 / 100 == 97.0), the second does not. The bid
        # boundary reconciles a stated price against this same expression, through
        # `list_price()`, so the two cannot round differently and call it fraud.
        resulting_price = list_price * (100.0 - pct) / 100.0
        floor = self.price_floor(product_ref)
        if resulting_price + WALL_TOLERANCE < floor:
            rule = self.envelope_ref(f"floors#{product_ref}")
            return (REASON_BELOW_PRICE_FLOOR, floor, rule), resulting_price, floor, cap
        return None, resulting_price, floor, cap

    def would_authorize(self, product_ref: str, requested_pct: Any) -> bool:
        """Whether the envelope authorizes this depth *right now*, minting and logging nothing.

        The bid boundary asks this before admitting a grant it did not watch being minted. The
        ledger records what *was* authorized; a merchant who tightens the envelope is changing
        what *is*, and a grant obtained under the old cap must not survive the new one on the
        strength of a ledger entry alone.

        Read-only on purpose: a re-check that emitted would put a claim in the ledger every time
        the guard ran, which is the opposite of a guard.
        """
        try:
            pct = float(requested_pct)
        except (TypeError, ValueError):
            return False
        denial, _price, _floor, _cap = self._evaluate_discount(str(product_ref), pct)
        return denial is None

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

        **The depth goes through the envelope wall.** ``learned_policy['actions'][cluster]`` is
        the output of the store's own learning loop, not a merchant approval, and a loop that has
        learned to ask for 25% must still be refused by a 20% envelope. Copying `discount_pct`
        out of it verbatim made this hook a second door to a discount — one with no walls at all,
        reached without calling :meth:`authorize_discount`, and unnoticed because the claim's key
        is `policy_action`. A depth the envelope refuses fails closed to no discount and is
        recorded in the call log; the rest of the policy's choice stands, because only the depth
        is walled.
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
        walled = ""
        learned = self.learned_policy
        if isinstance(learned, Mapping):
            by_cluster = _as_mapping(learned.get("actions"), "learned policy actions")
            chosen = _as_mapping(by_cluster.get(cluster_id), "learned policy action")
            for name in ("commitment_keys", "value_prop"):
                if name in chosen:
                    action[name] = chosen[name]
            if "discount_pct" in chosen:
                proposed = chosen["discount_pct"]
                try:
                    depth = float(proposed)
                except (TypeError, ValueError):
                    depth = None
                if depth == 0.0:
                    # No depth is inside every wall, and asking about a product the policy did
                    # not name would refuse it for the wrong reason.
                    action["discount_pct"] = 0.0
                elif depth is not None and self.would_authorize(product_ref, depth):
                    action["discount_pct"] = depth
                else:
                    walled = f"; envelope refused the policy's {proposed!r}% depth"

        claim = mint_claim(
            key="policy_action",
            value=action,
            source=ProvenanceSource.learned_policy,
            ref=f"policy:{self.store_id}:{version}#{cluster_id}",
            observed_at=self._observed_at(),
        )
        emitted = self._emit([claim])
        self._log(
            "choose_policy_action",
            cluster_id,
            "action_walled" if walled else "action",
            emitted,
            detail=f"{version}{walled}",
        )
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
    "REASON_NON_FINITE_DISCOUNT",
    "REASON_OVER_MAX_DISCOUNT",
    "REASON_UNKNOWN_PRODUCT",
    "REASON_UNPRICEABLE_PRODUCT",
    "WALL_TOLERANCE",
    "Denied",
    "HookCall",
    "HookInputError",
    "ToolHooks",
]
