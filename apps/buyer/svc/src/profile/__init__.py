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

The k-anonymity floor, and why it is off by default
---------------------------------------------------
Coarsening is not anonymity. The bucket tuple is a deterministic function of the account, so
two buyers with different histories generally land on different tuples — measured on 2,400
generated buyers, ``build_buckets`` produces 2,142 distinct tuples and leaves 2,074 of them
(86.4%) alone in a class of one. A buyer alone in their class is re-linkable across pseudonym
rotations by whoever holds the tuple.

That is the **documented default**, not a defect. SPEC §Non-goals is explicit: "No
k-anonymity enforcement beyond a configurable floor (default 1 in fixtures; production knob
documented)", and SPEC A6 restates it — a floor above 1 is unsatisfiable in the small fixture
populations the suites run on, so fixtures get ``k = 1`` and production gets a knob.

This module is where the knob lives.

* :func:`k_anonymity_floor` reads :data:`K_ANONYMITY_ENV`
  (``PROXYSHOP_BUYER_K_ANONYMITY``) and returns :data:`DEFAULT_K_ANONYMITY` — ``1`` — when it
  is unset, empty or unparseable. A deployment that wants a floor sets that variable.
* :func:`anonymise_cohort` is the enforcement. At ``k <= 1`` it is exactly
  ``[build_buckets(a) for a in accounts]`` and nothing is generalised: today's behaviour,
  value for value. At ``k > 1`` it applies local-recoding generalisation over the whole
  release and returns buckets for which ``min(class size) >= k`` holds — for any population of
  at least ``k`` accounts, adversarial ones included.
* :func:`build_profiles` is the release path that pairs those buckets with pseudonyms.

Enforcement is necessarily a property of a **release over a population**, never of one
record: a release of one account is always unique, so no per-account function can promise a
floor. That is why the knob is honoured by the cohort builder and not by
:func:`build_profile`, which is handed a single buyer and can only publish what
:func:`build_buckets` computes.

Usage::

    from apps.buyer.svc.src.profile import build_profile, build_profiles
    profile = build_profile(account, vault.issue(account["email"]))   # one buyer, no floor
    profiles = build_profiles(accounts, pseudonyms)                   # a release, floor applied

Imports are relative on purpose; see :mod:`buyer_svc.vault` for why (this tree is reachable
as both ``buyer_svc.profile`` and ``apps.buyer.svc.src.profile``).
"""

from __future__ import annotations

import json
import os
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
    "BOTTOM_LEVEL",
    "BUCKET_KEYS",
    "BUDGET_BANDS",
    "CATEGORY_FALLBACK",
    "CATEGORY_LIMIT",
    "CATEGORY_MIN_SUPPORT",
    "CATEGORY_TAXONOMY",
    "COARSE_BUDGET_BANDS",
    "COARSE_FREQUENCY_TIERS",
    "DEFAULT_K_ANONYMITY",
    "FREQUENCY_TIERS",
    "IDENTITY_ACCOUNT_KEYS",
    "K_ANONYMITY_ENV",
    "TOP_BUDGET_BAND",
    "TOP_COARSE_BUDGET_BAND",
    "IdentityLeak",
    "anonymise_cohort",
    "buckets_at_level",
    "build_buckets",
    "build_profile",
    "build_profiles",
    "category_label",
    "coarsen_budget_band",
    "coarsen_categories",
    "coarsen_frequency_tier",
    "coarsen_region",
    "equivalence_class",
    "generalise_budget_band",
    "generalise_frequency_tier",
    "generalise_region",
    "identity_leaks",
    "k_anonymity_floor",
    "publish_profile",
    "taxonomy_affinity",
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

#: Every value :func:`coarsen_frequency_tier` can emit, and the order-count ceiling that
#: selects each. Exhaustive on purpose: :func:`identity_leaks` needs to know the coarseners'
#: closed vocabulary, and a vocabulary written down twice drifts.
FREQUENCY_TIERS: tuple[tuple[int | None, str], ...] = (
    (0, "none"),
    (2, "occasional"),
    (9, "regular"),
    (None, "frequent"),
)

# --------------------------------------------------------------------------------------
# the k-anonymity knob, and the generalisation vocabulary it spends
# --------------------------------------------------------------------------------------

#: The environment variable a deployment sets to raise the floor above the fixture default.
K_ANONYMITY_ENV = "PROXYSHOP_BUYER_K_ANONYMITY"

#: The floor when nothing is configured. **1 by SPEC §Non-goals** — "default 1 in fixtures;
#: production knob documented" — which is to say: no enforcement, exactly as before this knob
#: existed. Fixture populations are far too small to satisfy a floor above 1 (SPEC A6), so
#: raising this default would break every suite in the repo rather than protect anyone.
DEFAULT_K_ANONYMITY = 1

#: The generalised spend bands, each the union of two canonical ones. Only reachable when the
#: knob is set above 1; at the default no profile ever carries one of these.
COARSE_BUDGET_BANDS: tuple[str, ...] = ("0-100", "100-500", "500+")

#: The open-ended coarse band, named for callers that want the label rather than the tuple.
TOP_COARSE_BUDGET_BAND = "500+"

#: Canonical band -> coarse band. Written out rather than recomputed from the bounds so the
#: generalisation is a table someone can read and check, not an arithmetic coincidence.
_BUDGET_GENERALISATION: dict[str, str] = {
    "0-50": "0-100",
    "50-100": "0-100",
    "100-250": "100-500",
    "250-500": "100-500",
    "500-1000": "500+",
    TOP_BUDGET_BAND: "500+",
}

#: The generalised frequency tiers — ``occasional`` swallows ``regular``.
COARSE_FREQUENCY_TIERS: tuple[str, ...] = ("none", "occasional", "frequent")

#: Fine tier -> coarse tier.
_FREQUENCY_GENERALISATION: dict[str, str] = {
    "none": "none",
    "occasional": "occasional",
    "regular": "occasional",
    "frequent": "frequent",
}

#: Orders that must sit behind a taxonomy label before a *generalised* release publishes it.
#: A label carried by one order is not a habit, it is that order.
CATEGORY_MIN_SUPPORT = 2

#: The closed merchandising taxonomy a generalised ``category_affinity`` draws from. The
#: unconstrained default vocabulary is the account's own order slugs, which is the highest-
#: entropy thing in the tuple; generalising them onto a fixed list of eleven labels is the
#: single biggest reduction the ladder makes.
CATEGORY_TAXONOMY: tuple[str, ...] = (
    "footwear",
    "apparel",
    "outdoor",
    "electronics",
    "beauty",
    "home",
    "grocery",
    "media",
    "pets",
    "sports",
    "other",
)

#: The label an unrecognised slug generalises to. A real, crowded taxonomy member — "things
#: we have no coarse name for" — never a per-buyer bucket.
CATEGORY_FALLBACK = "other"

#: Slug token -> taxonomy label. Matched against the hyphen-separated *tokens* of a slug, and
#: tried in :data:`CATEGORY_TAXONOMY` order, so "running-shoes" lands in ``footwear`` rather
#: than ``sports`` and "category-of-the-month" is not filed under ``pets`` for containing
#: "cat".
_TAXONOMY_TOKENS: dict[str, tuple[str, ...]] = {
    "footwear": (
        "shoe",
        "shoes",
        "sneaker",
        "sneakers",
        "boot",
        "boots",
        "sandal",
        "sandals",
        "footwear",
        "trainers",
        "slipper",
        "slippers",
    ),
    "apparel": (
        "apparel",
        "clothing",
        "clothes",
        "shirt",
        "shirts",
        "tshirt",
        "jacket",
        "jackets",
        "dress",
        "dresses",
        "pants",
        "trousers",
        "sock",
        "socks",
        "hat",
        "hats",
        "outerwear",
        "knitwear",
        "denim",
        "jeans",
    ),
    "outdoor": (
        "outdoor",
        "outdoors",
        "trail",
        "camp",
        "camping",
        "tent",
        "tents",
        "hiking",
        "hike",
        "climbing",
        "backpack",
        "backpacks",
        "kayak",
        "ski",
        "snowboard",
    ),
    "electronics": (
        "electronics",
        "electronic",
        "headphone",
        "headphones",
        "headset",
        "laptop",
        "laptops",
        "phone",
        "phones",
        "camera",
        "cameras",
        "audio",
        "speaker",
        "speakers",
        "tablet",
        "monitor",
        "console",
        "computer",
    ),
    "beauty": (
        "beauty",
        "skincare",
        "cosmetics",
        "cosmetic",
        "fragrance",
        "perfume",
        "makeup",
        "haircare",
        "shampoo",
    ),
    "home": (
        "home",
        "kitchen",
        "cookware",
        "furniture",
        "bedding",
        "decor",
        "garden",
        "appliance",
        "appliances",
        "homeware",
        "lighting",
        "rug",
        "rugs",
        "tools",
    ),
    "grocery": (
        "grocery",
        "groceries",
        "coffee",
        "espresso",
        "tea",
        "snack",
        "snacks",
        "food",
        "wine",
        "beer",
        "beverage",
        "beverages",
        "pantry",
        "produce",
    ),
    "media": (
        "media",
        "book",
        "books",
        "game",
        "games",
        "music",
        "movie",
        "movies",
        "vinyl",
        "comics",
    ),
    "pets": ("pets", "pet", "dog", "dogs", "cat", "cats", "aquarium"),
    "sports": (
        "sports",
        "sport",
        "fitness",
        "yoga",
        "bike",
        "bikes",
        "cycling",
        "gym",
        "running",
        "workout",
        "golf",
        "tennis",
    ),
}

#: Rungs of the generalisation ladder, finest (0) to fully suppressed. Rung 0 **is**
#: :func:`build_buckets` — the default release — and each rung widens exactly one facet,
#: highest-entropy first, so a record is never made to give up two things when giving up one
#: would have put it in a crowd::
#:
#:     rung  budget_band     category_affinity   frequency_tier  region          first_time
#:     0     6 fine bands    <= 3 account slugs  4 fine tiers    country-subdiv  bool
#:     1     6 fine bands    <= 2 taxonomy       4 fine tiers    country         bool
#:     2     3 coarse bands  <= 1 taxonomy       4 fine tiers    country         bool
#:     3     3 coarse bands  <= 1 taxonomy       3 coarse tiers  country         bool
#:     4     3 coarse bands  <= 1 taxonomy       3 coarse tiers  None            bool
#:     5     3 coarse bands  []                  3 coarse tiers  None            bool
#:     6     None            []                  None            None            None
BOTTOM_LEVEL = 6

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


def generalise_budget_band(band: str | None) -> str | None:
    """A canonical band widened into its :data:`COARSE_BUDGET_BANDS` union.

    An unrecognised value is **suppressed**, not passed through: the point of widening is that
    the released vocabulary stays closed, and a value that is not in the fine table cannot be
    widened into a coarse one honestly.
    """
    if not isinstance(band, str):
        return None
    return _BUDGET_GENERALISATION.get(band.strip())


def _slug(value: str) -> str:
    return _SLUG_STRIP.sub("-", value.strip().casefold()).strip("-")


def _category_counts(account: Mapping[str, Any]) -> dict[str, int]:
    """Raw category slug -> how many of this account's orders carried it."""
    counts: dict[str, int] = {}
    for order in _orders(account):
        category = order.get("category")
        if not isinstance(category, str):
            continue
        slug = _slug(category)
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    return counts


def coarsen_categories(account: Mapping[str, Any], *, limit: int = CATEGORY_LIMIT) -> list[str]:
    """At most ``limit`` category slugs, most-bought first, ties broken alphabetically.

    Truncation is the coarsening: a store learns what the buyer keeps coming back for, not
    the shape of their whole order history.

    The vocabulary is open — these are the account's own slugs — which is fine at the default
    floor of 1 and is the first thing :func:`buckets_at_level` gives up when a floor is
    configured. See :func:`taxonomy_affinity`.
    """
    ranked = sorted(_category_counts(account).items(), key=lambda item: (-item[1], item[0]))
    return [slug for slug, _count in ranked[: max(0, limit)]]


def category_label(category: str) -> str:
    """The :data:`CATEGORY_TAXONOMY` label a raw category slug generalises to.

    >>> category_label("running-shoes")
    'footwear'
    >>> category_label("Trail Gear")
    'outdoor'
    >>> category_label("reclaimed-teak-sideboards")
    'other'
    """
    tokens = set(_slug(category).split("-")) if isinstance(category, str) else set()
    for label in CATEGORY_TAXONOMY:
        if tokens & set(_TAXONOMY_TOKENS.get(label, ())):
            return label
    return CATEGORY_FALLBACK


def taxonomy_affinity(
    account: Mapping[str, Any],
    *,
    limit: int = CATEGORY_LIMIT,
    min_support: int = CATEGORY_MIN_SUPPORT,
) -> list[str]:
    """At most ``limit`` closed-taxonomy labels the buyer has ``min_support`` orders behind.

    The generalised form of :func:`coarsen_categories`, and two coarsenings rather than one:

    * **vocabulary** — every value comes from :data:`CATEGORY_TAXONOMY`, so the account's own
      free text is not what identifies the class;
    * **support** — a label needs ``min_support`` orders behind it, so a single unusual
      purchase is dropped rather than published as a marker.

    Ranked by support, ties broken alphabetically.
    """
    labels: dict[str, int] = {}
    for slug, count in _category_counts(account).items():
        label = category_label(slug)
        labels[label] = labels.get(label, 0) + count
    ranked = sorted(
        ((label, count) for label, count in labels.items() if count >= max(1, min_support)),
        key=lambda item: (-item[1], item[0]),
    )
    return [label for label, _count in ranked[: max(0, limit)]]


def coarsen_frequency_tier(account: Mapping[str, Any]) -> str:
    """How often this buyer buys, as a tier — never as a count."""
    orders = len(_orders(account))
    for ceiling, tier in FREQUENCY_TIERS:
        if ceiling is None or orders <= ceiling:
            return tier
    raise AssertionError("FREQUENCY_TIERS must end in an open-ended tier")


def generalise_frequency_tier(tier: str | None) -> str | None:
    """A fine tier merged into its :data:`COARSE_FREQUENCY_TIERS` union, or ``None``."""
    if not isinstance(tier, str):
        return None
    return _FREQUENCY_GENERALISATION.get(tier.strip())


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


def generalise_region(region: str | None) -> str | None:
    """``"US-OR"`` -> ``"US"``: the country, with the subdivision dropped.

    Deliberately syntactic — it widens whatever :func:`coarsen_region` produced rather than
    re-deriving it from a table of country codes, so a coarsener rewired to return a postal
    code still carries that postal code up the ladder where :func:`identity_leaks` finds it,
    instead of being quietly mapped to ``None`` by a lookup miss.
    """
    if not isinstance(region, str):
        return None
    return region.split("-", 1)[0].strip() or None


def build_buckets(account: Mapping[str, Any]) -> ProfileBuckets:
    """The five coarse facets, each from its own coarsener. No other account key is read.

    This is rung 0 of the ladder and the default release: no k-anonymity floor is applied,
    per SPEC §Non-goals. :func:`anonymise_cohort` is where a configured floor is spent.
    """
    return ProfileBuckets(
        budget_band=coarsen_budget_band(account),
        category_affinity=coarsen_categories(account),
        frequency_tier=coarsen_frequency_tier(account),
        region=coarsen_region(account.get("region")),
        first_time=not _orders(account),
    )


# --------------------------------------------------------------------------------------
# the configurable floor (SPEC §Non-goals: "default 1 in fixtures; production knob
# documented"). Everything below is inert at the default.
# --------------------------------------------------------------------------------------


def k_anonymity_floor(env: Mapping[str, str] | None = None) -> int:
    """The configured floor, or :data:`DEFAULT_K_ANONYMITY` when nothing asks for one.

    Args:
        env: environment mapping to read; defaults to :data:`os.environ`. Passing a plain
            dict is how tests and callers configure a floor without touching the process.

    A value that is not a positive integer is treated as "unset" rather than raising. A
    profile builder is not the right place to fail a boot over a typo in an optional knob, and
    the failure mode of falling back is the documented default rather than an unsafe one.

    >>> k_anonymity_floor({})
    1
    >>> k_anonymity_floor({"PROXYSHOP_BUYER_K_ANONYMITY": "5"})
    5
    >>> k_anonymity_floor({"PROXYSHOP_BUYER_K_ANONYMITY": "banana"})
    1
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(K_ANONYMITY_ENV)
    if raw is None:
        return DEFAULT_K_ANONYMITY
    try:
        configured = int(str(raw).strip())
    except ValueError:
        return DEFAULT_K_ANONYMITY
    return configured if configured >= 1 else DEFAULT_K_ANONYMITY


def buckets_at_level(account: Mapping[str, Any], level: int) -> ProfileBuckets:
    """The five facets, generalised to ``level`` of the ladder. Rung 0 is :func:`build_buckets`.

    The rungs are the table on :data:`BOTTOM_LEVEL`. They are *monotone*: every facet at rung
    ``n+1`` is at least as coarse as the same facet at rung ``n``, which is what makes
    "generalise this record one rung" a sound move for :func:`anonymise_cohort` — a class can
    only ever grow.

    No account key is read except through a named coarsener, exactly as at rung 0; the ladder
    widens the coarseners' output, it does not reach around them.
    """
    if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= BOTTOM_LEVEL:
        raise ValueError(f"generalisation level must be 0..{BOTTOM_LEVEL}, got {level!r}")
    if level == 0:
        return build_buckets(account)
    if level >= BOTTOM_LEVEL:
        return ProfileBuckets(
            budget_band=None,
            category_affinity=[],
            frequency_tier=None,
            region=None,
            first_time=None,
        )

    band = coarsen_budget_band(account)
    tier = coarsen_frequency_tier(account)
    region = generalise_region(coarsen_region(account.get("region")))
    affinity = taxonomy_affinity(account, limit=2)

    if level >= 2:
        band = generalise_budget_band(band)
        affinity = affinity[:1]
    if level >= 3:
        tier = generalise_frequency_tier(tier)
    if level >= 4:
        region = None
    if level >= 5:
        affinity = []

    return ProfileBuckets(
        budget_band=band,
        category_affinity=affinity,
        frequency_tier=tier,
        region=region,
        first_time=not _orders(account),
    )


def equivalence_class(buckets: Any) -> tuple[Any, ...]:
    """The quasi-identifier tuple two buyers must share to be in one anonymity class.

    Exported because "how many buyers share this profile" is the measurement a floor turns on,
    and a measurement each auditor has to re-derive drifts.
    """
    data = _serialise(buckets)
    if not isinstance(data, Mapping):
        raise ValueError(f"buckets must be a mapping or a model, got {type(buckets).__name__}")
    affinity = data.get("category_affinity")
    return (
        data.get("budget_band"),
        tuple(affinity) if isinstance(affinity, (list, tuple)) else (),
        data.get("frequency_tier"),
        data.get("region"),
        data.get("first_time"),
    )


def _classes(ladder: Sequence[Sequence[ProfileBuckets]], levels: Sequence[int]):
    """Group record indices by the class of the rung each is currently released at."""
    grouped: dict[tuple[Any, ...], list[int]] = {}
    for index, level in enumerate(levels):
        grouped.setdefault(equivalence_class(ladder[index][level]), []).append(index)
    return grouped


def anonymise_cohort(
    accounts: Sequence[Mapping[str, Any]], *, k: int | None = None
) -> list[ProfileBuckets]:
    """Buckets for a whole release, one per account, honouring the configured floor.

    At ``k <= 1`` — the default, per SPEC §Non-goals — this is exactly
    ``[build_buckets(account) for account in accounts]``. Nothing is generalised, nothing is
    suppressed, and the output is value-for-value what the single-account builder produces.

    At ``k > 1`` it applies local-recoding generalisation. Every record starts on rung 0; any
    equivalence class smaller than ``k`` has its members generalised one rung; repeat. Rungs
    are finite and monotone, so this reaches a fixpoint in at most :data:`BOTTOM_LEVEL`
    passes. Whatever is still short is then folded into the fully-suppressed bottom class,
    smallest class first, until nothing is below ``k``.

    The guarantee at ``k > 1`` is unconditional for ``len(accounts) >= k``: in the worst case
    every record ends in the bottom class, which then holds every record. Utility is not
    spent to get there — a record is generalised only while it is *actually* in a class too
    small, so buyers in crowded classes keep their region and their affinity.

    Args:
        accounts: the whole release. Order is preserved in the result.
        k: the floor. ``None`` reads :func:`k_anonymity_floor`.

    Raises:
        ValueError: ``k`` is below 1, or a floor above 1 was asked of a release with fewer
            than ``k`` accounts — which no generalisation can satisfy, and saying so is
            better than returning something that merely looks anonymous.
    """
    floor = k_anonymity_floor() if k is None else k
    if floor < 1:
        raise ValueError(f"k must be at least 1, got {floor}")

    records = list(accounts)
    if floor == 1:
        return [build_buckets(account) for account in records]
    if not records:
        return []
    if len(records) < floor:
        raise ValueError(
            f"a release of {len(records)} account(s) cannot be {floor}-anonymous: no "
            f"generalisation can put {floor} buyers in a class that has fewer than {floor}"
        )

    ladder = [
        [buckets_at_level(account, level) for level in range(BOTTOM_LEVEL + 1)]
        for account in records
    ]
    levels = [0] * len(records)

    for _pass in range(BOTTOM_LEVEL + 1):
        deficient = [
            index
            for members in _classes(ladder, levels).values()
            if len(members) < floor
            for index in members
        ]
        if not deficient:
            break
        moved = False
        for index in deficient:
            if levels[index] < BOTTOM_LEVEL:
                levels[index] += 1
                moved = True
        if not moved:
            break

    # Residual suppression. Anything still short is folded into the bottom class, which every
    # suppressed record shares; each fold strictly shrinks the set of un-suppressed records,
    # so this terminates — and it terminates satisfied, because the bottom class ends up
    # holding every record and ``len(records) >= floor``.
    while True:
        grouped = _classes(ladder, levels)
        if all(len(members) >= floor for members in grouped.values()):
            break
        movable = [
            members
            for members in grouped.values()
            if any(levels[index] < BOTTOM_LEVEL for index in members)
        ]
        if not movable:  # pragma: no cover - unreachable while len(records) >= floor
            raise AssertionError("every record is suppressed and the bottom class is still short")
        for index in min(movable, key=len):
            levels[index] = BOTTOM_LEVEL

    return [ladder[index][level] for index, level in enumerate(levels)]


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


#: Every label the closed-vocabulary coarseners can emit. These values are chosen by
#: :func:`coarsen_budget_band` and :func:`coarsen_frequency_tier` from a fixed table, never
#: copied out of the account, so no account could have leaked into one of them.
#:
#: The generalised tables join them on the same argument and for the same reason: a buyer
#: whose email local part is ``home@`` or ``sports@`` must not have their profile refused
#: because a *fixed* taxonomy label happens to spell their address. None of these values is
#: reachable at the default floor of 1, so adding them changes nothing about what today's
#: releases are checked against.
_CLOSED_VOCABULARY: frozenset[str] = frozenset(
    {label for _low, _high, label in BUDGET_BANDS}
    | {TOP_BUDGET_BAND}
    | {tier for _ceiling, tier in FREQUENCY_TIERS}
    | set(COARSE_BUDGET_BANDS)
    | set(COARSE_FREQUENCY_TIERS)
    | set(CATEGORY_TAXONOMY)
)


def _account_category_slugs(account: Mapping[str, Any]) -> set[str]:
    """The slugs :func:`coarsen_categories` may legitimately emit for ``account``.

    A slug here came from ``orders[].category`` — the buyer's merchandising taxonomy, which
    is not an identity field and which the allowlist publishes on purpose.
    """
    slugs: set[str] = set()
    for order in _orders(account):
        category = order.get("category")
        if isinstance(category, str):
            slug = _slug(category)
            if slug:
                slugs.add(slug)
    return slugs


def _incidental_bucket_values(account: Mapping[str, Any]) -> set[str]:
    """Bucket values that cannot be a disclosure however they got into the profile.

    Substring matching an account's identity fragments against the whole serialized profile
    is the right *shape* for a backstop and the wrong granularity on its own: it cannot tell
    a coarsener that copied the postal code into ``region`` from a coarsener that emitted its
    own canonical label which happens to spell a word out of the buyer's address.

    The second is not hypothetical and it is not rare. ``none@example.com`` has no orders, so
    ``frequency_tier`` is ``"none"``, so the local part of their address appears inside their
    own profile and the build refuses — that is the default state of *every* brand-new
    signup. ``espresso.fan@example.com`` who buys espresso is the same collision one bucket
    over. Both used to fail closed, and failing closed on a value the account could not have
    supplied is a denial of service, not a privacy guarantee.

    So two families of value are held out of the haystack, and only these two:

    * anything in :data:`_CLOSED_VOCABULARY` — a label from a fixed table;
    * a slug of one of *this* account's own order categories.

    Everything else — a region code, an unrecognised string, anything a rewired coarsener
    invents — is still matched in full.
    """
    return set(_CLOSED_VOCABULARY) | _account_category_slugs(account)


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
    incidental = _incidental_bucket_values(account)
    # Each bucket value is searched on its own. Joining them first made a fragment able to
    # match across the seam between two unrelated values, which is a leak report about a
    # string no bucket ever held.
    searchable = [
        text.casefold() for text in _text_values(body) if text.strip().casefold() not in incidental
    ]
    leaked = {value for value in fragments if any(value in text for text in searchable)}
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


def build_profiles(
    accounts: Sequence[Mapping[str, Any]],
    pseudonyms: Sequence[str],
    *,
    k: int | None = None,
) -> list[BuyerProfile]:
    """A ``BuyerProfile`` per account, as a **release** rather than one buyer at a time.

    Identical to calling :func:`build_profile` in a loop while the floor is at its default of
    1. It differs only when a floor is configured: because this sees the whole population it
    can enforce one, which a per-account builder structurally cannot.

    Args:
        accounts: the release. Order is preserved in the result.
        pseudonyms: one per account, echoed verbatim, positionally paired.
        k: the floor. ``None`` reads :func:`k_anonymity_floor`.

    Raises:
        ValueError: the sequences differ in length, an account is not a mapping, a pseudonym
            is empty, or the release is too small for the configured floor.
        IdentityLeak: as :func:`build_profile`, for any account in the release.
    """
    records = list(accounts)
    names = list(pseudonyms)
    if len(records) != len(names):
        raise ValueError(
            f"every account needs its own pseudonym: {len(records)} accounts, {len(names)} names"
        )
    for account in records:
        if not isinstance(account, Mapping):
            raise ValueError(f"account must be a mapping, got {type(account).__name__}")
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a profile needs the pseudonym its session was issued")

    released = anonymise_cohort(records, k=k)
    profiles: list[BuyerProfile] = []
    for account, name, buckets in zip(records, names, released, strict=True):
        profile = BuyerProfile(pseudonym=name, buckets=buckets)
        leaked = identity_leaks(profile, account)
        if leaked:
            raise IdentityLeak(
                f"R5: buyer identity reached the store-facing profile for pseudonym "
                f"{name!r}: {leaked!r}"
            )
        profiles.append(profile)
    return profiles


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
