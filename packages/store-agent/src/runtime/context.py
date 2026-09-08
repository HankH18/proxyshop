"""Store-context assembly for one auction, laid out for prompt caching.

Two jobs, and they are the same job seen from two ends.

**Reading the context.** The advocate is handed a store context (envelope, catalog, pixel feed,
learned policy, network priors) and a `BidRequest`, in whichever shape the caller has them:
plain dicts from a fixture, pydantic models from the wire, or a mixture. Everything downstream
asks this module instead of re-deriving "is it a model or a dict" at each site.

**Ordering it.** DESIGN pins store-agent prompts as *static-context-first for prompt caching*,
which is a statement about ORDER: a cache prefix is only reusable while the bytes in front of
the changing part do not move. :meth:`AuctionContext.cache_layout` publishes that order as
three tiers — what is stable for the store, what is stable for the run, and what is new for
this request — so the prompt-building tickets share one layout instead of each inventing one,
and so the boundary between "cacheable" and "not" is a thing that can be asserted.

The bid path itself calls no LLM and reads no clock: prices come from the catalog and claims
come from the hooks (R8/S4). The layout exists for the pitch/rationale work that sits beside
the bid, not underneath it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from contracts.boundary import parse_timestamp
from contracts.signing import canonical_json

#: The envelope key carrying the cold-start intro discount, as a percentage depth.
#:
#: DESIGN's cold-start rule is "list price, envelope standing commitments, intro discount rule
#: if the envelope defines one". `contracts.Envelope` pins no such field and forbids extras, so
#: an intro rule can only arrive on the raw context mapping the merchant service hands over —
#: which is where this reads it from. Absent (the usual case, and the fixture case) means no
#: intro rule and therefore no discount. The depth still goes through `authorize_discount`
#: like every other: naming a rule in an envelope is not the same as clearing the walls.
INTRO_DISCOUNT_KEY = "intro_discount_pct"

#: The store-context key stating how long an offer this agent makes stands, as an ISO instant.
#:
#: `contracts.boundary.validate_bid` refuses a bid whose offer states no expiry at all —
#: "an offer with no stated expiry is an offer nobody can price the risk of" — so this is not
#: optional decoration, it is what makes a bid admissible. It arrives on the context because an
#: expiry is a MERCHANT decision and because computing one would mean reading a clock, which
#: ends byte-identical reproduction (S4). When the context states none, the advocate falls back
#: to the request's own `respond_by`; see :meth:`AuctionContext.offer_expires_at`.
OFFER_EXPIRES_AT_KEY = "offer_expires_at"

#: The store-context key stating how long an offer this agent makes STANDS, as seconds.
#:
#: The second way a merchant can answer the same question :data:`OFFER_EXPIRES_AT_KEY` answers,
#: and the only one that survives being asked twice. That key takes an absolute instant, so the
#: single thing a merchant can state with it is one fixed timestamp — correct for one auction
#: and stale by the next. :meth:`AuctionContext.offer_expires_at` has always said "a store that
#: wants its offers to outlive the auction says so, once, on its context", and until this key
#: existed that sentence pointed at a mechanism with no working form: not one of the four
#: shipped demo contexts states an expiry, every one of them falls through to `respond_by`, and
#: every real offer on the demo was therefore dead the moment the auction closed. Measured on
#: the served buyer route: all four sponsored rows expired **3.2 seconds** after the shortlist
#: was handed over.
#:
#: A DURATION is not the invented "+48h" the fallback's docstring rejects, and the difference is
#: who chose the number. `respond_by + 48h` would be the agent committing its merchant to a
#: window nobody approved; `respond_by + <this>` is the merchant's own approved number, stated
#: once, in the only shape a repeated auction can use. The agent still invents nothing.
#:
#: Absent means absent: the conservative `respond_by` floor is unchanged for every context that
#: does not state one, which is every context written before this key existed. Present but
#: unreadable, or zero, or negative, is a DECLINE and never a silent fall-through — see
#: :meth:`AuctionContext.offer_expires_at` for why, and it is the same rule an unreadable
#: absolute expiry has always had.
OFFER_VALID_FOR_SECONDS_KEY = "offer_valid_for_seconds"

#: "The context did not mention a window at all", told apart from every value it could mention.
#:
#: A private sentinel rather than `None`, because `None` is a thing a merchant can write into
#: JSON and `0` is a thing a merchant can mean. Reading the key with `or` would fold both into
#: "absent" and hand a merchant who wrote `0` the conservative floor instead of the decline
#: their statement earns.
_MISSING_WINDOW = object()

#: The store-context key carrying pitches that were ALREADY SERVED, as ``{bid_offer_id: text}``.
#:
#: R15/S3 promise that a replay reproduces what was served. A pitch is written by a language
#: model, so it is the one thing in a bid that a replay cannot recompute — regenerate it and the
#: replay produces a *different, equally plausible* message, and the divergence is silent because
#: nothing about the new text looks wrong. So a pitch is an ARTIFACT: generated once, carried on
#: `Bid.message`, stored, and thereafter replayed as an INPUT.
#:
#: This is the key a replay harness (or an operator's context file) populates to hand a stored
#: pitch back to the agent; :func:`~.pitch.compose_pitch` returns it verbatim and calls no model
#: at all. It is read at the same trust level as the envelope, because it arrives by the same
#: route — the merchant service's own store context — and the envelope already decides the
#: discount cap.
SERVED_PITCHES_KEY = "served_pitches"

#: The store-context keys that may carry the store's own registered domain, in priority order,
#: read off the context first and off the approved envelope second.
#:
#: **Why the agent needs it at all.** `contracts.Offer.checkout_url` is the field the exchange's
#: C10/D22 check reads, and an offer that states none is excluded from every shortlist
#: (`apps/exchange/tests/test_ranking.py::test_an_offer_with_no_checkout_url_is_not_shortlisted`)
#: — measured end to end as `ranked: []`, every store excluded `off_domain_checkout`. Nothing in
#: this package used to write the field, so a hosted agent's every bid was unshortlistable.
#:
#: **Why it is CONFIGURATION and not a derivation.** The URL's host is compared, by exact
#: equality, against the domain the *platform* holds for this seller
#: (`apps/exchange/src/checkout/sellers.py`). A domain this module synthesised — `f"{store_id}
#: .example.com"`, say — would be a fact no evidence supports, exactly like the invented currency
#: `AuctionContext.currency` refuses to mint, and it would pass the check only by coincidence of
#: two independent guesses agreeing. So the merchant states it, once, on the context it already
#: hands over; when it states none the offer carries no `checkout_url`, which is the legal R10
#: fallback shape (`apps/exchange/src/checkout/domain.py` calls absent and off-domain different
#: conditions, and refuses only the second).
STORE_DOMAIN_KEYS: tuple[str, ...] = ("store_domain", "domain")

#: The characters a DNS label may carry, once the host is lower-cased. Deliberately narrow —
#: letters, digits and the hyphen — which is exactly a DNS name, covers punycode (``xn--…``) and
#: an IPv4 literal, and refuses two shapes measured coming out of an earlier draft of
#: :func:`store_domain_host`:
#:
#: * ``[::1]`` — `urlsplit` strips the brackets, so the host came back ``::1`` and the URL built
#:   on it (``https://::1/cart/…``) had **no parsable host at all**. The offer looked configured
#:   and the exchange refused it, which is the worst of both.
#: * ``a b.com`` — a space survived, and `urlsplit` is lenient enough that the platform's own
#:   host comparison then said *on-domain* about a string no browser can dial.
_HOST_LABEL_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

#: The schemes a stated domain may wear. A bare host is the normal spelling; ``https://…`` and
#: ``http://…`` are the two an operator writes by habit and both name the same host. Anything
#: else — ``ftp://``, ``javascript://``, ``not-a-scheme://`` — is refused rather than having its
#: authority quietly harvested: a value that was not a web address is a value the merchant did
#: not mean as one, and silently reading ``evil.tld`` out of ``javascript://evil.tld`` is the
#: same class of mistake as reading the userinfo out of a userinfo spoof.
_ACCEPTED_DOMAIN_SCHEMES = frozenset({"", "http", "https"})


def _is_dns_label(label: str) -> bool:
    """One dot-separated component of a host: LDH, and never leading or trailing with a hyphen.

    The hyphen rule is RFC 1035's and it is not pedantry here — ``-a.com`` and ``a-.com`` are
    not names anybody can register, so a context stating one is a context with a typo in it, and
    the honest answer is no checkout URL rather than one pointing at a name that cannot resolve.
    """
    return (
        bool(label)
        and set(label) <= _HOST_LABEL_CHARACTERS
        and not label.startswith("-")
        and not label.endswith("-")
    )


def store_domain_host(value: Any) -> str | None:
    """The bare, lower-cased hostname a checkout URL may be built on, or `None`.

    Accepts what an operator actually writes — ``store-alpha.example.com`` and
    ``https://store-alpha.example.com`` both yield ``store-alpha.example.com`` — and refuses
    everything that is not purely a DNS host: a path, a query, a fragment, userinfo, a port, an
    IPv6 literal, or a character a DNS label cannot carry. Those are refused rather than silently
    trimmed, because trimming would publish a checkout URL the merchant did not write while still
    looking configured; a `None` here leaves the offer with no `checkout_url` at all, which is a
    condition the exchange already reads correctly.

    Lower-cased and de-dotted to the same spelling `checkout/domain.py` normalises the registered
    domain to, so the two strings the platform compares cannot differ by case or a trailing dot.
    """
    if isinstance(value, bool) or value is None:
        # `str(True)` is `"true"`, which is a syntactically valid host. Excluded explicitly, the
        # same way `as_number` excludes it, so a boolean flag that landed in the wrong key
        # cannot become a domain.
        return None
    text = str(value).strip()
    if not text:
        return None
    if any(character.isspace() or character < " " or character == "\x7f" for character in text):
        # `urlsplit` STRIPS ASCII tab, CR and LF before parsing, so `"store.example.com\nX"`
        # came back as the host `store.example.comx` — a domain the merchant does not own,
        # published by a value they did not write. This function's contract is refusal, never
        # silent repair, so the check happens on the RAW text before `urlsplit` can launder it.
        return None
    try:
        parts = urlsplit(text if "//" in text else f"//{text}")
    except ValueError:
        return None
    if parts.scheme.lower() not in _ACCEPTED_DOMAIN_SCHEMES:
        return None
    if parts.path.strip("/") or parts.query or parts.fragment:
        return None
    try:
        if parts.username or parts.password or parts.port is not None:
            return None
        host = parts.hostname
    except ValueError:
        # A port that is not a number, or a bracketed host that does not parse.
        return None
    if not host:
        return None
    host = host.strip().lower().rstrip(".")
    labels = host.split(".")
    if not host or not all(_is_dns_label(label) for label in labels):
        return None
    # The round trip, checked rather than assumed: whatever is returned here becomes the
    # authority of a URL, and the platform compares `urlsplit(url).hostname`. If those two ever
    # disagree the agent publishes a URL that fails a check it believes it passes.
    if urlsplit(f"https://{host}/").hostname != host:  # pragma: no cover - defence in depth
        return None
    return host


def as_mapping(value: Any, what: str) -> dict[str, Any]:
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


def as_sequence(value: Any) -> list[Any]:
    """A list of entries, treating a bare mapping as a one-entry sequence."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return [value]
    return list(value)


def as_number(value: Any) -> float | None:
    """A finite number as a float, or `None` when the value is not one.

    `bool` is excluded deliberately: `True` is an `int` in Python, and an availability flag that
    silently became the number 1 would make "in_stock" comparable with a price.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        # `OverflowError` is the one a reader adds last and a merchant reaches first: an integer
        # with more than 308 digits is a thing `json.loads` produces from a context file, and
        # `float()` refuses it with an `ArithmeticError` that is neither a `TypeError` nor a
        # `ValueError`. It escaped `bid()`'s own `_UNUSABLE_INPUT` tuple and surfaced as a raw
        # traceback rather than a decline that names the field — measured on a 401-digit
        # `offer_valid_for_seconds`, and reproducible on `intro_discount_pct` long before this
        # key existed.
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / ±inf
        return None
    return number


#: The calendar this runtime can state an instant in — RFC-3339's own, years 1 through 9999 —
#: expressed as EPOCH SECONDS — `0001-01-01T00:00:00Z` and `10000-01-01T00:00:00Z`.
#:
#: They are checked BEFORE any arithmetic, and that is not belt-and-braces. A merchant may
#: state a window of `1e306` seconds: `as_number` reads it (finite, positive), the sum with the
#: deadline is still finite, and then `epoch * 1000.0` overflows to `inf`, whose floor is `nan`,
#: which `int()` refuses. Measured before this guard existed — a real bid, a real context::
#:
#:     decline(unusable_store_context): ValueError: cannot convert float NaN to integer
#:
#: A decline, because an outer handler caught it, but the WRONG decline: it named the whole
#: context unusable and put a Python exception string in a merchant-facing detail, where the
#: honest answer is `unstatable_offer_expiry` and the merchant's own number. This pair decides
#: both whether the arithmetic may run and whether its answer is spellable.
#:
#: These bounds are the ONLY calendar check. An earlier draft also tested the computed
#: YEAR afterwards, which an adversarial review showed to be unreachable — no epoch that passes
#: the bounds can produce an out-of-range year — so it was removed rather than left as a branch
#: no input takes and a docstring nobody could verify.
#:
#: Verified against `datetime` in ``test_offer_validity_window``, because a hardcoded epoch
#: nobody checked is exactly the kind of constant that is wrong by a day.
_EPOCH_AT_YEAR_1 = -62_135_596_800.0
_EPOCH_AT_YEAR_10000 = 253_402_300_800.0


def _instant(epoch: float) -> str | None:
    """An epoch as the RFC-3339 string `contracts.Offer.expires_at` is typed as, or `None`.

    **Integer arithmetic by hand, and that is not an accident.**
    `test_the_runtime_reads_no_clock_and_no_randomness` refuses the `datetime` import anywhere
    on the bid path — "a clock read once a day is still a clock" — and it reads the SOURCE, so
    it cannot tell `datetime.fromtimestamp`, a pure conversion, from `datetime.now`, a clock.
    Being right about this one call is not a reason to make that gate blind to the next one, and
    the gate caught this import the first time it was written. S4's byte-identical reproduction
    is the property it protects and the property this method needs, so the honest move is to
    stop needing the import.

    It is also not duplicating a renderer this module could have called. The exchange's own
    (`exchange.auction.routes.rfc3339_deadline`) is not importable from here at all:
    `packages/store-agent/Dockerfile` copies `packages/contracts`, `proxyshop_support`,
    `packages/store-agent/src` and `packages/llm/src`, and nothing under `apps/` — so the hosted
    image does not contain it. (An earlier draft of this paragraph said an import linter forbade
    it. That was wrong, and an adversarial review checked: `.importlinter` holds two contracts,
    and the C3 one runs the other way — `source_modules = exchange` — so nothing there forbids
    `store_agent -> exchange`, and store-agent TESTS import the exchange freely. The packaging
    is the real reason and it is a harder one.) `contracts` publishes a reader
    (:func:`parse_timestamp`) and no writer; if a shared writer is ever wanted, beside that
    reader is where it goes, and this becomes a call.

    The conversion is the standard civil-from-days one, in whole milliseconds — the resolution
    the exchange publishes `respond_by` at, so an offer expiry carries no digits the deadline it
    was derived from could not. `None` for an instant outside the years RFC-3339 can spell,
    which is refused rather than clamped for the same reason an unusable window is: a merchant
    who stated it meant something, and no clamp chosen here would be the thing they meant.
    """
    if not _EPOCH_AT_YEAR_1 <= epoch < _EPOCH_AT_YEAR_10000:
        return None
    # Split BEFORE scaling, and round the fraction to microseconds exactly as
    # `datetime.fromtimestamp` does. The obvious `int(epoch * 1000.0 // 1.0)` is wrong and was:
    # at large epochs the product `epoch * 1000.0` is itself rounded to the nearest double,
    # which can cross a millisecond boundary the true value never reached. Measured over 300,000
    # random instants across years 1..9999, against the standard library: **2,349 disagreements**,
    # every one an off-by-one millisecond, the first at ``176048524741.948`` (year 7548) where
    # this rendered ``.948`` and the calendar says ``.947``.
    #
    # Splitting first keeps the whole seconds exact (they are integers well inside a double's
    # exact range for every year RFC-3339 can spell) and leaves a fraction in [0, 1), where a
    # double has precision to spare. Nothing on the bid path could reach the broken case —
    # `respond_by` parses to a millisecond multiple around 1.8e9 and the sums are exact there —
    # but "no caller reaches it today" is not the same claim as "it is right", and the docstring
    # above says this agrees with the calendar.
    whole_seconds = int(epoch // 1.0)
    microseconds = int(round((epoch - whole_seconds) * 1_000_000.0))
    if microseconds >= 1_000_000:  # a fraction that rounded up to a whole second
        whole_seconds += 1
        microseconds = 0

    days, seconds_of_day_total = divmod(whole_seconds, 86_400)
    ms_of_day = seconds_of_day_total * 1000 + microseconds // 1000

    # Howard Hinnant's `civil_from_days`, shifted to an era starting 0000-03-01 so leap days
    # land at the end of a cycle and the month arithmetic below has no special cases.
    z = days + 719_468
    era = (z if z >= 0 else z - 146_096) // 146_097
    day_of_era = z - era * 146_097
    year_of_era = (
        day_of_era - day_of_era // 1460 + day_of_era // 36_524 - day_of_era // 146_096
    ) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    shifted_month = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * shifted_month + 2) // 5 + 1
    month = shifted_month + 3 if shifted_month < 10 else shifted_month - 9
    if month <= 2:
        year += 1

    seconds_of_day, milliseconds = divmod(ms_of_day, 1000)
    minutes_of_day, second = divmod(seconds_of_day, 60)
    hour, minute = divmod(minutes_of_day, 60)
    return (
        f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}.{milliseconds:03d}Z"
    )


@dataclass(frozen=True)
class HardConstraint:
    """One R19 eligibility filter, read off the intent. `field`/`op`/`value`, never weighted."""

    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class AuctionContext:
    """One store's context joined to one `BidRequest`. Immutable, and free of any clock.

    Built by :func:`assemble_context`. Everything on it is derived from the two arguments; there
    is no ambient state, which is what lets two runs on identical inputs produce identical bids
    (S4).
    """

    auction_id: str
    store_id: str
    envelope: Mapping[str, Any]
    catalog: Mapping[str, Mapping[str, Any]]
    live_state: Mapping[str, Mapping[str, Any]]
    learned_policy: Any
    network_priors: Mapping[str, Any]
    intent: Mapping[str, Any]
    profile: Mapping[str, Any]
    respond_by: str
    store_currency: str | None
    stated_offer_expiry: str | None
    #: The store's own registered domain, normalised by :func:`store_domain_host`, or `None`
    #: when the merchant stated none. Defaulted so that adding it broke no caller that built an
    #: `AuctionContext` positionally. See :data:`STORE_DOMAIN_KEYS`.
    store_domain: str | None = None
    #: Pitches already served for this store, keyed by `bid_offer_id`. See
    #: :data:`SERVED_PITCHES_KEY`; read through :meth:`replayed_pitch`, never directly.
    served_pitches: Mapping[str, Any] = field(default_factory=dict)
    #: The product the exchange ASKED this store about — `BidRequest.product_ref`, the catalogue
    #: entry the platform's own retrieval rostered for this buyer's intent.
    #:
    #: It is a QUESTION, never a constraint on the answer. D55 buys an in-network store the right
    #: to make its own case, and that includes counter-proposing a different product; nothing on
    #: this dataclass or downstream of it refuses a bid that names something else. What it does
    #: is stop the advocate from answering about a product nobody asked about when it *can* answer
    #: the question put to it — see :func:`~store_agent.runtime.bidding._chosen`.
    #:
    #: `None` when the exchange named no product, which is every solicitation minted before the
    #: field existed: `product_ref` is optional and nullable on the wire (D58), so a pre-D58
    #: request is not malformed, it simply asked nothing in particular and the advocate picks for
    #: itself exactly as it always did. Defaulted, like :attr:`store_domain`, so that adding it
    #: broke no caller that built an `AuctionContext` positionally.
    solicited_product_ref: str | None = None
    #: The merchant's stated offer lifetime in seconds, RAW and unvalidated, or `None` when the
    #: context states none. `Any` rather than `float` on purpose: whether the value is usable is
    #: :meth:`offer_expires_at`'s decision, and coercing it here would turn "the merchant wrote
    #: something unreadable" into "the merchant wrote nothing" — different facts with different
    #: answers, one a decline and one the conservative floor. See
    #: :data:`OFFER_VALID_FOR_SECONDS_KEY`.
    #:
    #: Appended LAST, like :attr:`store_domain` and :attr:`solicited_product_ref` before it, so
    #: adding it breaks no caller that builds an `AuctionContext` positionally.
    stated_offer_window: Any = None

    @property
    def cluster_id(self) -> str:
        """The intent's cluster, or the empty string when it names none."""
        return str(self.intent.get("cluster_id") or "")

    @property
    def currency(self) -> str | None:
        """The currency the offer is priced in: the store's own, else the one asked for.

        The store's own comes first because a price is the store's assertion. The intent's is
        the fallback rather than the primary, and `None` is a legitimate answer — the protocol
        makes `Offer.currency` optional, and inventing one would be a fact no evidence supports.
        """
        if self.store_currency:
            return self.store_currency
        asked = self.intent.get("currency")
        return str(asked) if asked else None

    @property
    def hard_constraints(self) -> tuple[HardConstraint, ...]:
        """R19's eligibility filters, in the order the intent states them."""
        constraints: list[HardConstraint] = []
        for raw in as_sequence(self.intent.get("hard_constraints")):
            entry = as_mapping(raw, "hard constraint")
            name = str(entry.get("field") or "")
            op = str(getattr(entry.get("op"), "value", entry.get("op")) or "")
            if not name or not op:
                continue
            constraints.append(HardConstraint(field=name, op=op, value=entry.get("value")))
        return tuple(constraints)

    @property
    def pursued_clusters(self) -> tuple[str, ...]:
        return tuple(str(c) for c in as_sequence(self.envelope.get("pursue_clusters")))

    def pursues(self, cluster_id: str) -> bool:
        """Whether the approved envelope told this agent to pursue `cluster_id`.

        Fail-closed on an envelope that lists none, and on an intent that names none. The
        envelope is the merchant's authorization, and "pursue nothing" is a thing a merchant can
        mean; reading an empty list as "pursue everything" would turn the narrowest envelope
        into the widest one.
        """
        return bool(cluster_id) and cluster_id in self.pursued_clusters

    @property
    def intro_discount_pct(self) -> float:
        """The envelope's cold-start intro depth, or 0.0 when it defines no intro rule."""
        depth = as_number(self.envelope.get(INTRO_DISCOUNT_KEY))
        return depth if depth is not None and depth > 0.0 else 0.0

    @property
    def offer_expires_at(self) -> str | None:
        """When an offer made in this auction stops standing. Never computed from a clock.

        Three answers, in the merchant's order of preference:

        1. the merchant's own :data:`OFFER_EXPIRES_AT_KEY`, an absolute instant, when the context
           states one — unchanged, and still the most specific thing a merchant can say;
        2. otherwise :data:`OFFER_VALID_FOR_SECONDS_KEY`, a DURATION, added to the request's
           `respond_by` — the merchant's own number, stated once, in the shape that survives
           being asked again tomorrow;
        3. otherwise the request's `respond_by` alone: the offer stands at least as long as the
           auction it was solicited for.

        (3) is a FLOOR, and a deliberately conservative one — an offer that expires exactly when
        the auction closes is honest about what the agent was actually authorized to promise,
        where an invented "+48h" would be the agent committing the merchant to a window nobody
        approved.

        **(2) is what makes that sentence true rather than merely well-meant.** This docstring
        has always ended "a store that wants its offers to outlive the auction says so, once, on
        its context" — and until the duration key existed, it could not. The only sayable thing
        was one absolute timestamp, correct for a single auction and stale by the next, so no
        real deployment ever stated one: none of the four shipped demo contexts did, all four
        fell through to (3), and every real offer in the demo was dead the instant the auction
        closed. Measured on the served buyer route, all four sponsored rows: **expires in 3.2
        seconds**. A shortlist a person cannot read fast enough to click is not a market, and
        the exchange's honest ``409`` at accept time is the right answer to the wrong question.

        A duration is not the invented window (3) refuses, and the difference is who chose the
        number. The agent still invents nothing; it adds a number its merchant wrote down.

        Stated as the ISO instant `contracts.Offer.expires_at` is typed as, and **checked with
        the same `parse_timestamp` the boundary will use**, so an unreadable one is `None` here
        rather than an `offer_expiry_unparseable` refusal at the door. Passing a value through
        unread would make this method a place where a typo becomes the exchange's problem.

        `None` means no readable expiry exists, which :func:`~.bidding.bid` turns into a decline:
        an offer nobody can price the risk of is one the exchange refuses anyway, and refusing
        it here at least says why.
        A stated expiry that cannot be read does NOT fall through to `respond_by`. Falling back
        would turn a merchant's typo into "the offer dies when the auction closes", silently and
        with the wrong lifetime; the merchant asked for something specific and unreadable, and
        the honest answer is to say so.

        **WHO OWNS "how long must a shortlist offer stand", and why it is not this method.**
        The paragraph above is right about what this agent may promise and it says nothing at
        all about what a shortlist needs, and for a while nobody answered the second question:
        the exchange knew (it mints its own R10 stand-in offers with `deadline + 900 s`,
        `exchange.auction.collect.FALLBACK_OFFER_TTL_SECONDS`), never told a store, and the
        store guessed the one value that cannot work — an offer with zero usable lifetime, dead
        the instant the auction ends and before any shopper can look at it.

        The answer is that the EXCHANGE owns it, on both halves, and this fallback stays exactly
        as conservative as it is:

        * **Admission.** An offer that stood for the whole auction is live for that auction's
          shortlist. The exchange used to judge liveness against a clock read taken AFTER its
          own fan-out returned, so an auction that overran its window by 21 ms voided every
          honest bid in it and served the shopper nothing but the exchange's own list prices —
          measured on the deployed droplet, 2 runs in 12. That was the exchange charging a store
          for the exchange's own latency, and it is fixed where it was caused:
          `exchange.ranking.serving.rank_auction` now caps the judging instant at the
          `respond_by` this exchange itself published.
        * **Honesty after admission.** A shortlisted offer that really has lapsed is refused
          rather than sold: `exchange.checkout.provider` re-reads the offer's own expiry at
          accept time and answers `409 DiscountDoesNotApply` before minting anything. A
          shortlist that silently dropped every real bid could not say even that much.

          **That refusal has a shelf life, and a merchant's window must fit inside it.** The
          exchange keeps the auction record and its shortlist for
          `exchange.auction.state.AUCTION_TTL_SECONDS` (900 s) from the CLOSE, while an offer's
          expiry runs from `respond_by` — which the fan-out normally reaches a second or two
          after the close. So an offer stated at 900 s outlives the record that explains it, and
          a late click is answered `404 … is not in Redis … its 900s TTL has expired` instead.
          Measured: never a `409`, on any run. The demo contexts therefore state **600**, which
          leaves about five minutes in which a lapsed offer is refused BY NAME. A merchant
          stating more than the record's TTL is not refused here — it is their offer to make —
          but they should know the late shopper gets a vaguer answer.

        **What would change this, and what it would cost.** A store cannot state a lifetime it
        was never asked for, because `BidRequest` carries `respond_by` and nothing else about how
        long an answer must stand — and `BidRequest` is `additionalProperties: false`, so the
        exchange publishing the window it needs is a change to `packages/contracts` and to every
        agent that validates against it. That is the shape of the real fix, and it is a protocol
        decision rather than a defaulting decision. Until it is made, the honest floor is the one
        below: the merchant approved a window for exactly as long as the auction it was asked
        about, and the agent says so instead of inventing more.
        """
        if self.stated_offer_expiry:
            return (
                self.stated_offer_expiry
                if parse_timestamp(self.stated_offer_expiry) is not None
                else None
            )

        closes_at = parse_timestamp(self.respond_by) if self.respond_by else None
        if self.stated_offer_window is not None:
            # A stated window that cannot be used does NOT fall through to `respond_by`, for
            # exactly the reason a stated instant does not: falling back would turn a merchant's
            # typo into "the offer dies when the auction closes", silently and with the wrong
            # lifetime. The merchant asked for something specific and unusable, and the honest
            # answer is to say so — which `bid()` does, as `unstatable_offer_expiry`.
            seconds = as_number(self.stated_offer_window)
            if seconds is None or seconds <= 0.0 or closes_at is None:
                return None
            # `None` when the sum leaves the calendar RFC-3339 can spell — a window so wide it
            # names no instant. Refused rather than clamped, like every other unusable
            # statement here.
            stated = _instant(closes_at.timestamp() + seconds)
            if stated is None:
                return None
            # AND when the window is real but too small to survive the wire. Instants are
            # stated to the millisecond, so a positive window under one of them renders as the
            # deadline itself — the zero-length life this whole key exists to end, reached
            # SILENTLY from a bid rather than said out loud as a decline. Measured before this
            # check: a stated `0.0009` produced `expires_at` exactly equal to `respond_by`, and
            # with a sub-millisecond `respond_by` it produced an expiry ~0.7 ms BEFORE the
            # auction closed — an offer the exchange's own ranker would then exclude as expired,
            # for a merchant who had asked for a longer life and got a shorter one.
            #
            # Compared through `parse_timestamp` rather than against `seconds`, because what has
            # to be true is a property of the STRING that leaves here: the offer stands strictly
            # past the close, as read by the same parser the boundary will use.
            landed = parse_timestamp(stated)
            if landed is None or landed.timestamp() <= closes_at.timestamp():
                return None
            return stated

        if closes_at is not None:
            return self.respond_by
        return None

    def checkout_url_for(
        self, product_ref: Any, variant_ref: Any = None, *, quantity: int = 1
    ) -> str | None:
        """Where a buyer completes this offer, on the store's own registered domain.

        The D22 cart-permalink shape — ``https://<domain>/cart/<variant>:<qty>`` — which is what
        ``apps/exchange/src/checkout/codes.py::build_cart_permalink`` builds and what the
        published contract's own example shows. No ``?discount=`` on it: the single-use code is
        minted by the exchange at accept time, and an agent that appended one would be asserting
        an authorization nobody granted.

        Variant-scoped when the catalog names a variant (D25: cart permalinks are), and scoped to
        the product otherwise — a store whose catalog carries no variant ids still gets a URL a
        browser can be sent to, and the alternative (no URL) costs it the shortlist.

        `None` when the merchant stated no domain, or stated one that is not a bare host. Clock
        free and RNG free like everything else on this path, so two runs on one context produce
        the same URL (S4).
        """
        if not self.store_domain:
            return None
        handle = str(variant_ref if variant_ref else product_ref or "").strip()
        if not handle:
            return None
        return f"https://{self.store_domain}/cart/{quote(handle, safe='')}:{int(quantity)}"

    def replayed_pitch(self, offer_ref: Any) -> str | None:
        """The pitch already served for this offer, verbatim — or `None` if there is none.

        Verbatim is the whole contract (rule F of :mod:`~.pitch`). Nothing here trims, screens or
        normalises the stored text: what was served is what a replay must reproduce, and an agent
        that "tidied" a stored artifact on the way back out would reproduce something else while
        reporting success. It is refused only when it is not text, or is blank — neither of which
        is a pitch anybody served.

        A missing or malformed `served_pitches` mapping is simply "no stored pitch", so a context
        that has never been through a replay behaves exactly as it does today.
        """
        stored = self.served_pitches
        if not isinstance(stored, Mapping):
            return None
        text = stored.get(str(offer_ref or ""))
        return text if isinstance(text, str) and text.strip() else None

    @property
    def is_cold(self) -> bool:
        """R10: the store has learned nothing yet, so the deterministic default applies."""
        return self.learned_policy is None

    def cache_layout(self) -> tuple[tuple[str, str, str], ...]:
        """``(tier, name, canonical_json)`` blocks, most stable first.

        Three tiers, ordered so a prompt built by concatenating them keeps the longest possible
        unchanged prefix between two calls:

        ``store``
            the merchant's own configuration — identity, approved envelope, catalog. Changes
            when the merchant changes something, which is rarely and deliberately.
        ``session``
            what the platform knows right now — the pixel feed, the learned policy, the network
            priors. Changes between runs, not between the requests inside one.
        ``request``
            this auction: its id, the intent, the buyer's coarse profile. New every time.

        Serialized with RFC 8785 canonical JSON, the same spelling `claim_fingerprint` uses, so
        two logically identical contexts produce byte-identical blocks and a cache hit is a
        property of the content rather than of dict insertion order.
        """
        blocks: list[tuple[str, str, Any]] = [
            ("store", "store_id", self.store_id),
            ("store", "envelope", self.envelope),
            ("store", "catalog", self.catalog),
            ("session", "live_state", self.live_state),
            ("session", "learned_policy", self.learned_policy),
            ("session", "network_priors", self.network_priors),
            ("request", "auction_id", self.auction_id),
            ("request", "intent", self.intent),
            ("request", "profile", self.profile),
        ]
        return tuple((tier, name, canonical_json(value)) for tier, name, value in blocks)


def _catalog(raw: Any) -> dict[str, dict[str, Any]]:
    return {
        str(key): as_mapping(value, "catalog entry")
        for key, value in as_mapping(raw, "catalog").items()
    }


def _solicited_ref(value: Any) -> str | None:
    """`BidRequest.product_ref` as a usable catalog key, or `None` when it is not one.

    Defensive for the same reason :func:`~store_agent.runtime.bidding._variant_ref` is: the
    request is whatever crossed the wire, and this value is about to be compared against catalog
    keys. Only a non-empty `str` counts. An int, a mapping or a `True` that became ``"123"`` or
    ``"{'a': 1}"`` or ``"True"`` would be a ref naming nothing, and a ref naming nothing is
    indistinguishable downstream from a store that cannot sell what was asked for — it would
    silently look like a legitimate counter-proposal instead of the malformed input it is.
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None


def assemble_context(request: Any, context: Any) -> AuctionContext:
    """Join a `BidRequest` to a store context. Pure, clock-free, and shape-tolerant.

    Both arguments may be plain mappings or pydantic models; nested members may be either.
    Unknown keys are ignored rather than refused — the context is assembled by the merchant
    service and the runtime is not the schema police for it — but every key this module reads is
    read here, once, so there is exactly one place to look for "what does the advocate use".
    """
    ctx = as_mapping(context, "store context")
    req = as_mapping(request, "bid request")
    envelope = as_mapping(ctx.get("envelope"), "envelope")
    currency = ctx.get("currency")
    stated_expiry = ctx.get(OFFER_EXPIRES_AT_KEY) or envelope.get(OFFER_EXPIRES_AT_KEY)
    # Read with a MISSING sentinel rather than `or`, because `0` is a value a merchant can write
    # and `0 or envelope.get(...)` would read it as absent — and a zero-second window is exactly
    # the statement that has to reach the decline branch rather than the fallback.
    stated_window = ctx.get(OFFER_VALID_FOR_SECONDS_KEY, _MISSING_WINDOW)
    if stated_window is _MISSING_WINDOW:
        stated_window = envelope.get(OFFER_VALID_FOR_SECONDS_KEY, _MISSING_WINDOW)
    stated_window = None if stated_window is _MISSING_WINDOW else stated_window
    domain = next(
        (
            host
            for source in (ctx, envelope)
            for key in STORE_DOMAIN_KEYS
            if (host := store_domain_host(source.get(key))) is not None
        ),
        None,
    )
    return AuctionContext(
        auction_id=str(req.get("auction_id") or ""),
        store_id=str(ctx.get("store_id") or envelope.get("store_id") or ""),
        envelope=envelope,
        catalog=_catalog(ctx.get("catalog")),
        live_state={
            str(key): as_mapping(value, "live-state entry")
            for key, value in as_mapping(ctx.get("live_state"), "live_state").items()
        },
        learned_policy=ctx.get("learned_policy"),
        network_priors=as_mapping(ctx.get("network_priors"), "network_priors"),
        intent=as_mapping(req.get("intent"), "intent"),
        profile=as_mapping(req.get("profile"), "buyer profile"),
        respond_by=str(req.get("respond_by") or ""),
        store_currency=str(currency) if currency else None,
        stated_offer_expiry=str(stated_expiry) if stated_expiry else None,
        stated_offer_window=stated_window,
        store_domain=domain,
        served_pitches=as_mapping(ctx.get(SERVED_PITCHES_KEY), "served pitches"),
        solicited_product_ref=_solicited_ref(req.get("product_ref")),
    )


def normalized(value: Any) -> str:
    """A comparable spelling of an attribute value: stripped and case-folded."""
    return str(value).strip().casefold()


def same_value(observed: Any, wanted: Any) -> bool:
    """Equality for catalog attributes: numeric when both are numbers, textual otherwise."""
    left, right = as_number(observed), as_number(wanted)
    if left is not None and right is not None:
        return left == right
    if isinstance(observed, bool) or isinstance(wanted, bool):
        return bool(observed) is bool(wanted)
    return normalized(observed) == normalized(wanted)


def satisfies(op: str, observed: Any, wanted: Any) -> bool:
    """Whether `observed` satisfies the R19 constraint ``op wanted``.

    An operator this function does not implement returns False — the constraint is NOT
    satisfied — rather than True or a raised error. A hard constraint is an eligibility filter,
    and the failure mode of an unknown filter must be "this product does not qualify", never
    "the filter did not apply".
    """
    if op == "eq":
        return same_value(observed, wanted)
    if op in ("lte", "gte"):
        left, right = as_number(observed), as_number(wanted)
        if left is None or right is None:
            return False
        return left <= right if op == "lte" else left >= right
    if op == "in":
        if isinstance(wanted, (str, bytes)) or not isinstance(wanted, Sequence):
            return same_value(observed, wanted)
        return any(same_value(observed, item) for item in wanted)
    if op == "contains":
        if isinstance(observed, str):
            return normalized(wanted) in normalized(observed)
        if isinstance(observed, Sequence):
            return any(same_value(item, wanted) for item in observed)
        return False
    return False


__all__ = [
    "INTRO_DISCOUNT_KEY",
    "OFFER_EXPIRES_AT_KEY",
    "OFFER_VALID_FOR_SECONDS_KEY",
    "SERVED_PITCHES_KEY",
    "STORE_DOMAIN_KEYS",
    "AuctionContext",
    "HardConstraint",
    "as_mapping",
    "as_number",
    "as_sequence",
    "assemble_context",
    "normalized",
    "same_value",
    "satisfies",
    "store_domain_host",
]
