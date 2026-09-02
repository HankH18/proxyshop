"""``buyer_svc.profile`` — the only buyer object a store is ever shown (T-070, SPEC R5).

R5 has two halves. :mod:`buyer_svc.vault` owns the first (the pseudonym rotates and is never
reissued); this module owns the second: **whatever a store receives carries no identity**.

The store-facing object is DESIGN §Data models' ``BuyerProfile`` — ``{pseudonym, buckets}``
and nothing else, where ``buckets`` holds exactly the five coarse facets
``{budget_band, category_affinity, frequency_tier, region, first_time}``.

Built by allowlist, never by redaction
--------------------------------------
The tempting implementation copies the account and deletes the identity keys. That is a
denylist, and a denylist is wrong the day someone adds ``account["shipping_contact"]``: the
new field is not on the list, so it ships. So :func:`build_profile` never touches the
account dict as a whole. Each bucket is produced by its own named function that reads one
or two named account keys and returns a *coarsened* value — a band rather than a total, a
tier rather than a count, an ISO region rather than an address. A field nobody wrote a
coarsener for cannot reach the output at all.

Coarsening is enforcement, not decoration. Each of these refuses input rather than passing
it through:

* :func:`coarsen_region` returns ``None`` for ``"97205"`` and for
  ``"44 Alder Way, Portland OR 97205"`` — only ISO-shaped country / subdivision codes pass.
* :func:`coarsen_budget_band` admits only the canonical band labels; a raw amount is snapped
  into a band and anything else is dropped rather than echoed.
* :func:`coarsen_categories` keeps at most :data:`CATEGORY_LIMIT` slugs, so an order history
  cannot be reconstructed from the affinity list.

:func:`identity_leaks` is the backstop behind the allowlist and :func:`build_profile` runs it
on every profile it builds: if any identity **value** from the account survived into the
serialized profile, the build raises :class:`IdentityLeak` instead of returning it.

Usage::

    from apps.buyer.svc.src.profile import build_profile
    profile = build_profile(account, vault.issue(account["email"]))

Imports are relative on purpose; see :mod:`buyer_svc.vault` for why (this tree is reachable
as both ``buyer_svc.profile`` and ``apps.buyer.svc.src.profile``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

try:  # `contracts` — the flat-src namespace, via the tracked `.pkgroot/contracts` symlink.
    from contracts import BuyerProfile, ProfileBuckets
except ModuleNotFoundError:  # pragma: no cover - exercised by the frozen acceptance suite
    # The frozen suite bootstraps ONLY the repo root onto `sys.path`, so `.pkgroot` is not
    # there and the flat-src spelling does not resolve. `packages.contracts` inserts
    # `.pkgroot` itself and then forwards to the very same module objects, so both branches
    # bind one `BuyerProfile` class rather than two look-alikes.
    from packages.contracts import BuyerProfile, ProfileBuckets  # type: ignore[no-redef]

__all__ = [
    "BUCKET_KEYS",
    "BUDGET_BANDS",
    "CATEGORY_LIMIT",
    "IDENTITY_ACCOUNT_KEYS",
    "TOP_BUDGET_BAND",
    "IdentityLeak",
    "build_buckets",
    "build_profile",
    "coarsen_budget_band",
    "coarsen_categories",
    "coarsen_frequency_tier",
    "coarsen_region",
    "identity_leaks",
    "publish_profile",
]

#: The five facets DESIGN §Data models pins on ``BuyerProfile.buckets``. Exactly these.
BUCKET_KEYS: tuple[str, ...] = (
    "budget_band",
    "category_affinity",
    "frequency_tier",
    "region",
    "first_time",
)

#: Canonical budget bands, low bound inclusive / high bound exclusive. The ONLY labels that
#: may appear in ``buckets["budget_band"]``.
BUDGET_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 50.0, "0-50"),
    (50.0, 100.0, "50-100"),
    (100.0, 250.0, "100-250"),
    (250.0, 500.0, "250-500"),
    (500.0, 1000.0, "500-1000"),
)

#: The open-ended top band.
TOP_BUDGET_BAND = "1000+"

#: Most categories an affinity list may carry. A longer list starts to be a purchase log.
CATEGORY_LIMIT = 3

#: Account keys that carry identity. Read by :func:`identity_leaks` — never by a coarsener.
IDENTITY_ACCOUNT_KEYS: tuple[str, ...] = (
    "account_id",
    "address",
    "buyer_id",
    "customer_id",
    "device_id",
    "email",
    "first_name",
    "full_name",
    "given_name",
    "ip_address",
    "last_name",
    "phone",
    "postal_code",
    "street",
    "surname",
    "user_id",
    "zip_code",
)

#: Below this length a value is too short to be a meaningful identifier and matching on it
#: produces noise ("OR" would collide with a region code). Identity values shorter than this
#: are not leak-checked; nothing in the allowlist can emit one anyway.
_MIN_LEAKABLE = 4

_REGION_SEPARATORS = re.compile(r"[-_/,\s]+")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class IdentityLeak(AssertionError):
    """A buyer identity value survived into a store-facing profile (R5).

    Raised by :func:`build_profile`, never caught inside this module. A profile that leaks is
    not degraded, it is disclosive, and returning it would be the failure R5 exists to
    prevent.
    """


# --------------------------------------------------------------------------------------
# coarseners — each reads named account keys and returns a bucket value or ``None``
# --------------------------------------------------------------------------------------


def _order_totals(account: Mapping[str, Any]) -> list[float]:
    """Every numeric order total on the account, oldest-first order preserved."""
    totals: list[float] = []
    for order in _orders(account):
        total = order.get("total") if isinstance(order, Mapping) else None
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            continue
        totals.append(float(total))
    return totals


def _orders(account: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    orders = account.get("orders")
    if not isinstance(orders, Sequence) or isinstance(orders, (str, bytes)):
        return []
    return [order for order in orders if isinstance(order, Mapping)]


def _band_for(amount: float) -> str:
    for low, high, label in BUDGET_BANDS:
        if low <= amount < high:
            return label
    return TOP_BUDGET_BAND if amount >= BUDGET_BANDS[-1][1] else BUDGET_BANDS[0][2]


def coarsen_budget_band(account: Mapping[str, Any]) -> str | None:
    """The buyer's spend band — a canonical label, never a figure.

    Three inputs are accepted and in this order:

    1. ``account["budget_band"]`` when it is already one of the canonical labels;
    2. ``account["budget_band"]`` when it parses as an amount — snapped into its band;
    3. otherwise the median order total, snapped into its band.

    Anything else is **dropped**. A free-text band ("mid-range", or an address someone put
    in the wrong field) is not echoed into a store-facing object just because it arrived
    under a plausible key.
    """
    labels = {label for _low, _high, label in BUDGET_BANDS} | {TOP_BUDGET_BAND}
    raw = account.get("budget_band")
    if isinstance(raw, str) and raw.strip() in labels:
        return raw.strip()
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return _band_for(float(raw))
    if isinstance(raw, str):
        try:
            return _band_for(float(raw.strip()))
        except ValueError:
            pass  # not a band label and not an amount -> fall through to the order history
    totals = sorted(_order_totals(account))
    if not totals:
        return None
    return _band_for(totals[len(totals) // 2])


def _slug(value: str) -> str:
    return _SLUG_STRIP.sub("-", value.strip().casefold()).strip("-")


def coarsen_categories(account: Mapping[str, Any], *, limit: int = CATEGORY_LIMIT) -> list[str]:
    """At most ``limit`` category slugs, most-bought first, ties broken alphabetically.

    Truncation is the coarsening: a store learns what the buyer keeps coming back for, not
    the shape of their whole order history.
    """
    counts: dict[str, int] = {}
    for order in _orders(account):
        category = order.get("category")
        if not isinstance(category, str):
            continue
        slug = _slug(category)
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [slug for slug, _count in ranked[: max(0, limit)]]


def coarsen_frequency_tier(account: Mapping[str, Any]) -> str:
    """How often this buyer buys, as a tier — never as a count."""
    orders = len(_orders(account))
    if orders == 0:
        return "none"
    if orders <= 2:
        return "occasional"
    if orders <= 9:
        return "regular"
    return "frequent"


def coarsen_region(value: Any) -> str | None:
    """An ISO-shaped ``COUNTRY`` or ``COUNTRY-SUBDIVISION`` code, or ``None``.

    Stores need enough geography to know whether they can ship; they do not need an address.
    So only alphabetic 2-3 character codes pass, at most two of them, and everything else is
    refused rather than trimmed:

    >>> coarsen_region("US-OR")
    'US-OR'
    >>> coarsen_region("97205") is None
    True
    >>> coarsen_region("44 Alder Way, Portland OR 97205") is None
    True

    Refusing is deliberate. A "best effort" coarsener that returned ``"44"`` for a street
    address, or the postal code for a postal code, would satisfy the shape of a bucket while
    defeating its purpose.
    """
    if not isinstance(value, str):
        return None
    kept: list[str] = []
    for part in _REGION_SEPARATORS.split(value.strip()):
        if not part:
            continue
        if len(kept) == 2:
            break
        if not part.isalpha() or not (2 <= len(part) <= 3):
            return None
        kept.append(part.upper())
    return "-".join(kept) or None


def build_buckets(account: Mapping[str, Any]) -> ProfileBuckets:
    """The five coarse facets, each from its own coarsener. No other account key is read."""
    return ProfileBuckets(
        budget_band=coarsen_budget_band(account),
        category_affinity=coarsen_categories(account),
        frequency_tier=coarsen_frequency_tier(account),
        region=coarsen_region(account.get("region")),
        first_time=not _orders(account),
    )


# --------------------------------------------------------------------------------------
# the backstop
# --------------------------------------------------------------------------------------


def _identity_values(account: Mapping[str, Any]) -> set[str]:
    """Every case-folded identity value on ``account`` worth matching on.

    An address is also split into its words, so a profile that shipped ``"Portland"`` out of
    ``"44 Alder Way, Portland OR 97205"`` is caught even though the whole string is not
    present verbatim.
    """
    found: set[str] = set()

    def add(text: str) -> None:
        text = text.strip().casefold()
        if len(text) >= _MIN_LEAKABLE:
            found.add(text)

    for key in IDENTITY_ACCOUNT_KEYS:
        value = account.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        add(value)
        for word in re.split(r"[\s,]+", value):
            add(word)
        if key == "email" and "@" in value:
            local, _, domain = value.partition("@")
            add(local)
            add(domain)
            for word in re.split(r"[.\-_+]+", local):
                add(word)
    return found


def _text_values(value: Any, _depth: int = 0) -> Iterable[str]:
    """Every string *value* reachable inside a plain nested structure.

    Keys are deliberately skipped. ``BuyerProfile`` and ``ProfileBuckets`` are both
    ``extra="forbid"``, so the key set is fixed by the contract and cannot carry buyer data —
    while scanning keys would make a buyer whose surname happened to be "Region" fail to log
    in. Only values can leak, so only values are searched.
    """
    if _depth > 12:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for sub in value.values():
            yield from _text_values(sub, _depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for sub in value:
            yield from _text_values(sub, _depth + 1)


#: Hex digits. See :func:`_distinguishable_from_entropy`.
_HEX_ALPHABET = frozenset("0123456789abcdef")


def _distinguishable_from_entropy(value: str) -> bool:
    """Could ``value`` appearing inside a pseudonym be anything but coincidence?

    A pseudonym is 32 hex characters of entropy. A short all-hex identity fragment — the
    postal code ``97205``, say — turns up inside one by chance roughly once in a couple of
    thousand logins, and treating that as a disclosure would fail real logins at random. A
    fragment carrying any non-hex character, or eight-plus characters long, cannot plausibly
    arrive by chance and is a genuine finding. Bucket values are searched for *every*
    fragment regardless; this narrowing applies only to the pseudonym itself.
    """
    return len(value) >= 8 or not set(value) <= _HEX_ALPHABET


def identity_leaks(profile: Any, account: Mapping[str, Any]) -> list[str]:
    """Identity values from ``account`` that appear anywhere inside ``profile``.

    Empty means clean. This is the backstop behind the allowlist in :func:`build_buckets`,
    and it refuses things the allowlist alone would not notice — a coarsener rewired to
    return the postal code, or a caller who passes the buyer's email as the pseudonym::

        >>> account = {"email": "dana.reyes@example.com", "postal_code": "97205"}
        >>> identity_leaks({"pseudonym": "psn-1", "buckets": {"region": "97205"}}, account)
        ['97205']
    """
    data = _serialise(profile)
    pseudonym: Any = None
    body: Any = data
    if isinstance(data, Mapping):
        pseudonym = data.get("pseudonym")
        body = {key: value for key, value in data.items() if key != "pseudonym"}

    fragments = _identity_values(account)
    haystack = "  ".join(_text_values(body)).casefold()
    leaked = {value for value in fragments if value in haystack}
    if isinstance(pseudonym, str):
        name = pseudonym.casefold()
        leaked |= {
            value for value in fragments if _distinguishable_from_entropy(value) and value in name
        }
    return sorted(leaked)


def _serialise(value: Any) -> Any:
    """Reduce a pydantic model (or anything model-ish) to plain data."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    return value


# --------------------------------------------------------------------------------------
# the public builder
# --------------------------------------------------------------------------------------


def build_profile(account: Mapping[str, Any], pseudonym: str) -> BuyerProfile:
    """The store-facing ``BuyerProfile`` for ``account``, under ``pseudonym``.

    Args:
        account: the buyer's record. May carry every identity field there is; none of them
            are read by a coarsener, and :func:`identity_leaks` proves none of them escaped.
        pseudonym: the pseudonym this session issued — echoed verbatim. It is the *only*
            thing tying the profile to a buyer, and :mod:`buyer_svc.vault` is the only place
            that can undo the tie.

    Returns:
        ``BuyerProfile(pseudonym=..., buckets=...)`` — top-level keys exactly
        ``{pseudonym, buckets}``, bucket keys exactly :data:`BUCKET_KEYS`.

    Raises:
        ValueError: ``account`` is not a mapping, or ``pseudonym`` is empty.
        IdentityLeak: an identity value from ``account`` reached the profile. Never
            swallowed — a leaking profile is not returned in a degraded form.
    """
    if not isinstance(account, Mapping):
        raise ValueError(f"account must be a mapping, got {type(account).__name__}")
    if not isinstance(pseudonym, str) or not pseudonym.strip():
        raise ValueError("a profile needs the pseudonym its session was issued")

    profile = BuyerProfile(pseudonym=pseudonym, buckets=build_buckets(account))

    leaked = identity_leaks(profile, account)
    if leaked:
        raise IdentityLeak(
            f"R5: buyer identity reached the store-facing profile for pseudonym "
            f"{pseudonym!r}: {leaked!r}"
        )
    return profile


def publish_profile(connection: Any, profile: BuyerProfile) -> None:
    """Upsert ``profile`` into ``app.buyer_accounts`` — the store-visible working set.

    The row is keyed by pseudonym and holds only the buckets, so every role that can read
    ``app.*`` (``exchange``, ``trust_rw``, ``app``) sees a buyer without seeing a person.
    The email side of the pair never leaves ``vault.*``.

    ``connection`` must be authenticated as ``app``: T-011's grant model gives ``buyer_vault``
    only ``SELECT`` on ``app.*``, so the vault role deliberately cannot write here. Two
    schemas, two roles, and no single connection that can join them.
    """
    dumped = profile.model_dump()
    with connection.cursor() as cur:
        cur.execute(
            "insert into app.buyer_accounts (pseudonym, buckets) values (%s, %s::jsonb) "
            "on conflict (pseudonym) do update set buckets = excluded.buckets",
            (dumped["pseudonym"], json.dumps(dumped["buckets"], sort_keys=True)),
        )
