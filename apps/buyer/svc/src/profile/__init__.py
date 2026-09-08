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

The k-anonymity floor, where it is spent, and why it is still off by default
---------------------------------------------------------------------------
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
record: a release of one account is always unique, so no per-account function can conjure a
floor out of a single buyer. What :func:`build_profile` *can* do — and now does (T-221,
T-363) — is read the floor and be told which other accounts are released beside this one:

* at ``k == 1`` it publishes rung 0, value for value what it published before;
* at ``k > 1``, given a ``cohort`` large enough to satisfy the floor, it publishes the rung
  :func:`anonymise_cohort` would have put this record on — the two share
  :func:`_release_levels`, so the login path and the cohort builder cannot drift apart;
* at ``k > 1`` with a release too small for the floor, it **withholds**: every facet
  suppressed (:data:`UNSATISFIABLE_FLOOR_LEVEL`). It does not raise, and it never falls back
  to the fine-grained tuple. :func:`build_profile` documents why that direction.

``MagicLinkAuth.profile_for`` — the only caller production has — reads the floor and hands
over its account directory as the cohort, so ``GET /buyer/profile`` is where a configured
floor is actually spent. Before this the ladder had no production call site at all: it was
correct code behind a caller that never called it.

The **default is still 1** (:data:`DEFAULT_K_ANONYMITY`), which is no enforcement, because
SPEC §Non-goals pins it there and ``tests/test_profile_k_anonymity_knob.py`` asserts it. What
changed is reachability, not the default: a deployment that sets
``PROXYSHOP_BUYER_K_ANONYMITY`` now gets the ladder on the path it actually runs, where
before it got rung 0 whatever it set.

Usage::

    from apps.buyer.svc.src.profile import build_profile, build_profiles
    profile = build_profile(account, vault.issue(account["email"]))   # floor read from env
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
    "UNSATISFIABLE_FLOOR_LEVEL",
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

#: The floor when nothing is configured. **1 by SPEC §Non-goals** (SPEC.md:56) — "default 1 in
#: fixtures; production knob documented" — restated by SPEC A6 (SPEC.md:84). No enforcement,
#: exactly as before this knob existed.
#:
#: T-221/T-363 asked whether this number should be raised. MEASURED across the buyer suite,
#: and the answer is that raising it is a different change from wiring the ladder:
#:
#: * The cost is a **step at k > 1, not a curve in k.** Simulating this constant at 5 fails 47
#:   nodes across six test files; at 2 it fails 46, and the two sets differ by a single node.
#:   There is no cheaper floor to pick, because what a raised default engages is not the
#:   ladder — it is :data:`UNSATISFIABLE_FLOOR_LEVEL`. Every caller in the tree except
#:   :meth:`MagicLinkAuth.profile_for` calls :func:`build_profile` with no ``cohort``, so the
#:   release is one record, and one record satisfies no floor above 1 by any generalisation.
#: * 41 of those 47 are behaviour that is meant to hold: 20 are R5 identity-backstop probes
#:   that go SILENT rather than red (a profile with nothing in it cannot leak, so the backstop
#:   stops being observable at all), 18 are consumers that need a published coarse value —
#:   ``region == "BEN"`` for the buyer named Ben, the truncated affinity list — and 3 require
#:   the release to vary per buyer at all. Only 5 pin the default itself.
#: * A floor is affordable only at population scale, and this constant cannot see the
#:   population. MEASURED on the suite's own generator: k=10 fully suppresses 100% of a
#:   30-buyer release and 55% of a 60-buyer one, while at 600 buyers it suppresses 3%.
#:
#: So the floor is spent through :data:`K_ANONYMITY_ENV`, by a deployment that knows how many
#: buyers it has. ``tests/test_profile_identity_floor.py`` pins the step, and
#: ``tests/test_profile_k_anonymity_knob.py`` pins this number against SPEC.
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

#: The longest a string can be and still be a merchandising slug. ``camera-lenses`` is 13
#: characters; ``gift-for-dana-reyes-44-alder-way-portland-97205`` is 47 and is a gift note
#: somebody typed into the category field. See :func:`_is_merchandising_slug`.
CATEGORY_SLUG_MAX_CHARS = 32

#: The most hyphen-separated tokens a merchandising slug may carry. Real ones are one to
#: three words; an address is nine.
CATEGORY_SLUG_MAX_TOKENS = 4

#: Two or more digits in a row. Postal codes, house numbers, phone numbers and account ids
#: all carry one; a merchandising slug does not need one, and treating "has a digit run" as
#: "is not a merchandising slug" costs nothing real and closes the highest-value fragment
#: family there is.
_DIGIT_RUN = re.compile(r"\d{2,}")

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

#: Rungs of the generalisation ladder, finest (0) to fully suppressed. Rung 0 emits what
#: :func:`build_buckets` emits — the default release — and each rung widens exactly one facet,
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

#: The rung a profile is published at when the configured floor **cannot be met** — a release
#: holding fewer than ``k`` records, which no generalisation can rescue, because an
#: equivalence class cannot hold more buyers than the release it is drawn from.
#:
#: The number is :data:`BOTTOM_LEVEL` and it is not a tuning choice: it is the only rung whose
#: class size does not depend on the population. Every record released here carries the same
#: fully-suppressed tuple, so "this class has at least k members" is satisfied by any release
#: that has k members at all — true for one buyer and for a million. Every finer rung would be
#: a guess about a population the caller cannot see, and guessing in that direction publishes
#: the fine-grained value the floor exists to withhold.
UNSATISFIABLE_FLOOR_LEVEL = BOTTOM_LEVEL

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
#: produces noise. Identity values shorter than this are not leak-checked.
#:
#: **Three, not four** (T-197). At four, a short name was not merely under-weighted, it was
#: invisible: ``first_name='Ann'``, ``last_name='Lee'`` and an order category of ``"ann lee
#: gear"`` published ``['ann-lee-gear']`` with the backstop reporting clean, because neither
#: fragment ever entered the haystack. A three-letter name is still the buyer's name.
#:
#: The constant gates four places and they must agree: what :func:`_identity_sources` records,
#: what :func:`_identity_fragments_in_slug` counts, what rule 3 of
#: :func:`_exempt_category_slugs` will accept as a merchandise token, and which fragments
#: :func:`identity_leaks` searches for in slug space. A fragment tracked on one side and
#: ignored on the other is how a slug becomes newly exempt without anybody deciding it should.
#:
#: The old value's stated reason for four was a collision with the region bucket. That reason
#: is real, but it was attached to the wrong thing — see :data:`_MIN_LEAKABLE_BY_BUCKET`, which
#: is where it now lives, because it is a fact about one bucket's alphabet and not about what
#: counts as an identifier anywhere else.
_MIN_LEAKABLE = 3

#: Buckets whose own vocabulary forces a *higher* floor than :data:`_MIN_LEAKABLE`.
#:
#: ``region`` is the only one, and it is not an exemption by another name — it is the same
#: length argument the global floor used to carry, applied where it is actually true.
#: :func:`coarsen_region` accepts alphabetic parts of **two or three** characters and refuses
#: everything else (``part.isalpha() and 2 <= len(part) <= 3``), so every string this bucket
#: can hold is an ISO-shaped code: ``US``, ``US-OR``, ``GB-ENG``, ``BEN``. A fragment short
#: enough to sit inside one is therefore colliding with the code's alphabet, not being
#: disclosed by it — and ``region`` has no per-value exemption at all, so a collision there
#: refuses the buyer permanently, for as long as their name and their country both stay what
#: they are.
#:
#: Measured, and this is why the number is 4 rather than 3: at 3 the buyer surnamed **Eng** in
#: ``GB-ENG`` is refused, and so are Ben in ``BEN``, Pan in ``PAN``, Nam in ``NAM``, Lao in
#: ``LAO`` and Che in ``CHE`` — all ordinary surnames, all permanently locked out of their own
#: profile. Four keeps every two- and three-letter code out of this bucket's haystack while
#: leaving the shortest real names trackable in every other bucket, which is what T-197 asked
#: for. A genuine disclosure through ``region`` — a postal code, a street, a city — cannot be
#: shorter than four characters and is unaffected: ``coarsen_region`` refuses anything with a
#: digit in it, so a postal code can only reach this bucket through a rewired coarsener, and
#: it is still found.
_MIN_LEAKABLE_BY_BUCKET: dict[str, int] = {"region": 4}

#: Identity keys that name a **person or an account** rather than a place. A merchandising
#: slug whose token spells one of these is not a coincidence — nobody's shop sells
#: ``dana-reyes`` — so the exemption in :func:`_exempt_category_slugs` never covers it.
#:
#: ``address`` and ``street`` are deliberately *not* here: place words genuinely collide with
#: merchandise (Park Lane and ``park-gear``, Beacon Street and ``beacon-lamps``), and
#: refusing those is the denial of service the exemption exists to prevent. Neither is
#: ``email``, which the buyer chooses themselves and which is why ``espresso.fan@example.com``
#: may buy espresso.
_NAMING_IDENTITY_KEYS: frozenset[str] = frozenset(IDENTITY_ACCOUNT_KEYS) - {
    "address",
    "email",
    "street",
}

#: How many of the account's own identity fragments one category slug may carry and still be
#: read as a coincidence. **One** — every case the exemption was ever added for is a single
#: collision (Park Lane and ``park-gear``, Cook and ``cookware``, ``espresso.fan@`` and
#: ``espresso``), and no legitimate merchandising slug has ever needed two.
#:
#: Two is where a coincidence stops being one. ``dana-reyes-gear``, ``danareyes-gear``,
#: ``espresso-fan-gear``, ``alder-way-portland-gear``, ``park-lane-gear`` — each is a
#: perfectly well-formed slug that spells the buyer, plus a word that does not, and counting
#: the buyer's fragments is the only thing that separates them from the collisions above.
#: Raising this to 2 reopens every one of them; lowering it to 0 is the denial of service
#: :func:`_exempt_category_slugs` exists to prevent.
_MAX_INCIDENTAL_FRAGMENTS = 1

_REGION_SEPARATORS = re.compile(r"[-_/,\s]+")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

#: Everything that is not a digit. Used by :func:`_identity_sources` to record a number's
#: digits with its grouping dropped — see the note on regrouping there (T-198).
_NON_DIGIT = re.compile(r"\D+")


class IdentityLeak(AssertionError):
    """A buyer identity value survived into a store-facing profile (R5).

    Raised by :func:`build_profile`, never caught inside this module. A profile that leaks is
    not degraded, it is disclosive, and returning it would be the failure R5 exists to
    prevent.

    The message names the **account keys** that leaked and never their values, and never the
    session subject either. An exception raised *because* identity escaped is read by a log
    sink and, if a route forgets to catch it, by an HTTP client: interpolating the values it
    is complaining about turned the backstop itself into the disclosure path it exists to
    close (T-133). :attr:`account_keys` carries the same information structurally, for a
    caller that wants to log it without parsing prose.
    """

    def __init__(self, message: str, *, account_keys: Sequence[str] = ()) -> None:
        super().__init__(message)
        #: The ``account`` keys whose values reached the profile. Names, never values.
        self.account_keys: tuple[str, ...] = tuple(account_keys)


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


def _coarsen(account: Mapping[str, Any]) -> ProfileBuckets:
    """Rung 0, with no backstop — the raw output of the five coarseners.

    Private. Together with :func:`_buckets_at_level`, which wraps it, these are the only two
    bucket-producing paths in this module that are not behind :func:`identity_leaks`, and both
    are private. They exist so :func:`anonymise_cohort` can build every rung of a record before
    anyone decides which rung is released: a record whose rung 0 leaks may still be released,
    clean, at rung 3, and refusing it while merely *considering* rung 0 would be a refusal
    about a value no store was ever going to be shown.

    Every PUBLIC entry point checks what it is about to emit — :func:`build_buckets`,
    :func:`buckets_at_level`, :func:`anonymise_cohort`, :func:`build_profile` and
    :func:`build_profiles`. If you add a sixth, guard it; that omission is the whole of T-164.
    """
    return ProfileBuckets(
        budget_band=coarsen_budget_band(account),
        category_affinity=coarsen_categories(account),
        frequency_tier=coarsen_frequency_tier(account),
        region=coarsen_region(account.get("region")),
        first_time=not _orders(account),
    )


def _refuse_if_leaking(emitted: Any, account: Mapping[str, Any]) -> None:
    """Raise :class:`IdentityLeak` if ``emitted`` carries any identity value of ``account``.

    The one line every public bucket-producing entry point runs before it returns (T-164). The
    refusal is :func:`_leak_report`'s, so it names account keys and never values, wherever it
    is raised from.
    """
    leaked = identity_leaks(emitted, account)
    if leaked:
        raise _leak_report(account, leaked)


def build_buckets(account: Mapping[str, Any]) -> ProfileBuckets:
    """The five coarse facets, each from its own coarsener. No other account key is read.

    This is rung 0 of the ladder and the default release: no k-anonymity floor is applied,
    per SPEC §Non-goals. :func:`anonymise_cohort` is where a configured floor is spent.

    Raises:
        IdentityLeak: an identity value from ``account`` reached the buckets. **This is a
            public entry point, so it is behind the R5 backstop** (T-164). It was not, and the
            asymmetry was a trap rather than a live defect: ``build_profile`` refused an
            account whose own order category spelled its surname while ``build_buckets``
            published ``['reyes-gear']`` for the same account, and the only thing keeping that
            off a store was that nothing yet called it. T-142 wires ``publish_profile``; a
            caller that sourced its buckets here instead of from ``build_profile`` would have
            bypassed the backstop entirely and nothing would have said so.
    """
    buckets = _coarsen(account)
    _refuse_if_leaking(buckets, account)
    return buckets


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
    """The five facets, generalised to ``level`` of the ladder, behind the R5 backstop.

    Raises:
        ValueError: ``level`` is not an integer rung in ``0..BOTTOM_LEVEL``.
        IdentityLeak: the buckets at ``level`` carry an identity value from ``account``.

    This is the THIRD public bucket-emitting entry point, and T-164 named only two. It is
    guarded anyway, because the argument the ticket makes about the other two is exactly as
    true here: it is public, it is in ``__all__``, it returns a ``ProfileBuckets`` that
    ``BuyerProfile`` accepts, and at rung 0 it returns the account's own free text. Measured
    before it was guarded: ``buckets_at_level(account, 0)`` on the contaminated fixture
    returned ``category_affinity=['running-shoes',
    'gift-for-dana-reyes-44-alder-way-portland-97205']`` and that value went through
    ``BuyerProfile`` into ``publish_profile`` and out to ``app.buyer_accounts`` with no refusal
    anywhere, while ``build_buckets``, ``build_profile`` and ``anonymise_cohort`` all refused
    the same account. That asymmetry did not exist before T-164's fix — rung 0 used to be
    ``build_buckets`` and inherited its guard — so guarding the other two without this one
    would have *created* the hole it was closing.

    The ladder itself is built through :func:`_buckets_at_level`, which is unguarded, so
    :func:`anonymise_cohort` can still consider a rung it does not release.
    """
    buckets = _buckets_at_level(account, level)
    _refuse_if_leaking(buckets, account)
    return buckets


def _buckets_at_level(account: Mapping[str, Any], level: int) -> ProfileBuckets:
    """The rung itself, with no backstop — see :func:`buckets_at_level` for the public one.

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
        return _coarsen(account)
    if level >= BOTTOM_LEVEL:
        return ProfileBuckets(
            budget_band=None,
            category_affinity=[],
            frequency_tier=None,
            region=None,
            first_time=None,
        )

    band = coarsen_budget_band(account)
    tier: str | None = coarsen_frequency_tier(account)
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
        IdentityLeak: an identity value from one of the ``accounts`` survived into the buckets
            released for it. **This is a public entry point, so it is behind the R5 backstop**
            (T-164) — see :func:`build_buckets` for why an unguarded one is a trap. The check
            is made against what this function actually **releases**, not against rung 0: a
            record whose rung-0 buckets carry a fragment may be released, clean, three rungs
            up, and refusing it for a value no store would have been shown would be a
            generalisation ladder that punishes the buyer it just protected.
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

    levels = _release_levels(records, floor)
    released = [
        _buckets_at_level(account, level) for account, level in zip(records, levels, strict=True)
    ]
    for account, buckets in zip(records, released, strict=True):
        _refuse_if_leaking(buckets, account)
    return released


def _release_levels(records: Sequence[Mapping[str, Any]], floor: int) -> list[int]:
    """The ladder rung each of ``records`` is released at, for a release satisfying ``floor``.

    Extracted so :func:`anonymise_cohort` and :func:`build_profile` cannot drift (T-221): a
    buyer served one at a time by the login path has to land on the rung the cohort builder
    would have put them on, or "the floor is enforced" means two different things depending on
    which entry point was asked.

    Every record starts on rung 0; every equivalence class smaller than ``floor`` has its
    members generalised one rung; repeat. The rungs are finite and monotone
    (:func:`_buckets_at_level`), so a class can only ever grow and this reaches a fixpoint in
    at most :data:`BOTTOM_LEVEL` passes. Whatever is still short afterwards is folded into the
    fully-suppressed bottom class, smallest class first, until nothing is below ``floor``.

    The caller's precondition is ``len(records) >= floor``, and it is what makes the residual
    fold terminate *satisfied* rather than merely terminate: in the worst case the bottom
    class ends up holding every record, and that class is then as big as the release.
    """
    ladder = [
        [_buckets_at_level(account, level) for level in range(BOTTOM_LEVEL + 1)]
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

    return levels


# --------------------------------------------------------------------------------------
# the backstop
# --------------------------------------------------------------------------------------


def _identity_sources(account: Mapping[str, Any]) -> dict[str, set[str]]:
    """Every case-folded identity fragment on ``account``, and which keys contributed it.

    An address is also split into its words, so a profile that shipped ``"Portland"`` out of
    ``"44 Alder Way, Portland OR 97205"`` is caught even though the whole string is not
    present verbatim.

    The *provenance* is what :func:`_exempt_category_slugs` and :func:`_leak_report` need and
    what a bare set of strings cannot give them: ``"dana"`` reached by way of ``first_name``
    is a name, and ``"park"`` reached by way of ``address`` is a place, and the two earn
    different answers from the backstop.

    Two normalisations are recorded beside the value as written, both closing a channel a
    fragment escaped through while the backstop reported clean (T-198):

    * **digits alone.** ``identity_leaks`` searches verbatim and in slug space, and a number
      *regrouped* rather than repunctuated is neither: the phone ``"555-0100"`` reaches a
      profile as ``gift-5550100-gear`` and no comparison in either space finds it. Which
      digits of a phone number are the phone number is the question the ticket left open, and
      the answer taken here is the conservative one — **all of them, in order, separators
      dropped** — because that is the only grouping-independent reading of a number, and it is
      exactly the transformation that hid it. Recorded under the key that contributed it, so
      the refusal still names ``phone`` rather than some synthetic ``phone_digits``.
    * **the email domain, word by word — except its last label.** The local part was already
      split and the domain was added whole, so ``reyes-family@example.com`` was refused and
      ``x@reyes-family.example`` — the same two words, one character to the right — rode out.
      The asymmetry was the defect, and splitting the domain removes it.

      The **final DNS label is dropped**, and that is not a detail. It is the one label nobody
      chooses: every ``.com`` account would otherwise contribute the fragment ``"com"``, which
      is inside the taxonomy tokens ``comics`` and ``computer``; every ``.org`` account would
      contribute ``"org"``, inside ``organic`` and ``organizers``. Because
      :data:`_MAX_INCIDENTAL_FRAGMENTS` is 1 and rule 5 of :func:`_exempt_category_slugs`
      charges that budget over the whole published list, one universal fragment plus one real
      collision withdraws the exemption for *every* slug on the account. Measured, before this
      was dropped: Cook buying ``cookware`` **and** ``computers`` was refused, and so was
      ``espresso.fan@example.com`` buying ``espresso`` and ``comics`` — the module's own two
      canonical coincidences, broken by one extra ordinary order. The words to the left of the
      last label are the ones a buyer can pick, and they are the ones a vanity domain spells.
    """
    found: dict[str, set[str]] = {}

    def add(text: str, key: str) -> None:
        text = text.strip().casefold()
        if len(text) >= _MIN_LEAKABLE:
            found.setdefault(text, set()).add(key)

    def add_digits(text: str, key: str) -> None:
        """The value's digits with every separator dropped — see the docstring."""
        digits = _NON_DIGIT.sub("", text)
        if digits != text.strip().casefold():
            add(digits, key)

    for key in IDENTITY_ACCOUNT_KEYS:
        value = account.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        add(value, key)
        add_digits(value, key)
        for word in re.split(r"[\s,]+", value):
            add(word, key)
            add_digits(word, key)
        if key == "email" and "@" in value:
            local, _, domain = value.partition("@")
            add(local, key)
            add(domain, key)
            for word in re.split(r"[.\-_+]+", local):
                add(word, key)
            # Every label but the last: the TLD is the one part of an address nobody picks.
            for label in domain.split(".")[:-1]:
                for word in re.split(r"[\-_+]+", label):
                    add(word, key)
    return found


def _identity_values(account: Mapping[str, Any]) -> set[str]:
    """Every case-folded identity value on ``account`` worth matching on."""
    return set(_identity_sources(account))


def _bucket_texts(
    value: Any, bucket: str | None = None, _depth: int = 0
) -> Iterable[tuple[str | None, str]]:
    """Every string *value* reachable inside a plain nested structure, with its bucket.

    Keys are deliberately never *matched against*. ``BuyerProfile`` and ``ProfileBuckets``
    are both ``extra="forbid"``, so the key set is fixed by the contract and cannot carry
    buyer data — while scanning keys would make a buyer whose surname happened to be
    "Region" fail to log in. Only values can leak, so only values are searched.

    Keys are *read* for one thing only: to say which of :data:`BUCKET_KEYS` a string was
    found under. That is what makes the exemptions below per-bucket rather than account-wide,
    which is the whole of T-139's second half — a value that is unremarkable inside
    ``category_affinity`` is a disclosure inside ``region``, and the flat walk this replaced
    could not tell the two apart. A string outside any bucket yields ``None`` and is
    therefore exempt from nothing.
    """
    if _depth > 12:
        return
    if isinstance(value, str):
        yield bucket, value
    elif isinstance(value, Mapping):
        for key, sub in value.items():
            child = key if isinstance(key, str) and key in BUCKET_KEYS else bucket
            yield from _bucket_texts(sub, child, _depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for sub in value:
            yield from _bucket_texts(sub, bucket, _depth + 1)


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

#: The same vocabulary, split by the bucket that can actually emit each label — because a
#: label is only unremarkable in the bucket whose coarsener owns it. ``"footwear"`` turning up
#: in ``region`` is not a taxonomy label, it is a coarsener that was rewired, and holding it
#: out of the haystack account-wide meant the backstop could not say so.
#:
#: ``category_affinity`` is deliberately **not** here (T-199), and it is the one bucket whose
#: exemption could not be justified the way the other two are. The justification is *"these
#: values are chosen by a coarsener from a fixed table, never copied out of the account"*.
#: That is true of ``budget_band`` and ``frequency_tier`` at every rung. It is true of
#: ``category_affinity`` only above the default floor, where :func:`taxonomy_affinity` picks
#: the label; at rung 0 — the default release, and the only release production makes — the
#: bucket carries the account's **own** ``orders[].category`` slugs verbatim. Roughly eleven
#: common English words are taxonomy labels, so a buyer whose surname is Home buying ``home``
#: published their surname and the backstop skipped the value without looking at it.
#:
#: Removing the entry does not refuse the coincidence, it just stops exempting it unread:
#: :func:`_exempt_category_slugs` is the exemption that bucket already has, it is earned per
#: slug rather than handed over per label, and it is the one that knows the difference between
#: Cook buying ``cookware`` and Home buying ``home``. A generalised release is unaffected in
#: practice for the same reason — a taxonomy label that collides with nothing on the account
#: is matched, found in nothing, and reported as nothing.
#:
#: Buckets absent here (``category_affinity``, ``region``, ``first_time``) have no *unconditional*
#: vocabulary exemption. ``category_affinity`` has a conditional one instead, applied in
#: :func:`identity_leaks` against :data:`_TAXONOMY_LABELS`: a label is incidental only when it is
#: not also one of this account's own order slugs.
_BUCKET_VOCABULARY: dict[str, frozenset[str]] = {
    "budget_band": frozenset(
        {label for _low, _high, label in BUDGET_BANDS}
        | {TOP_BUDGET_BAND}
        | set(COARSE_BUDGET_BANDS)
    ),
    "frequency_tier": frozenset(
        {tier for _ceiling, tier in FREQUENCY_TIERS} | set(COARSE_FREQUENCY_TIERS)
    ),
}

#: The closed taxonomy, as a set, for the conditional ``category_affinity`` exemption above.
_TAXONOMY_LABELS: frozenset[str] = frozenset(CATEGORY_TAXONOMY)


def _is_merchandising_slug(slug: str) -> bool:
    """Could ``slug`` be a category a shop actually sells under?

    ``orders[].category`` is free text and nothing validates it, which is the whole of T-139:
    an order whose category reads ``"gift for Dana Reyes, 44 Alder Way Portland 97205"``
    slugs to a 47-character, nine-token string carrying a house number and a postal code, and
    :func:`coarsen_categories` publishes it because it is what the account said it bought.

    Three cheap bounds separate a merchandising slug from a note somebody typed in the wrong
    field. Nothing in :data:`CATEGORY_TAXONOMY`, and nothing in any fixture or generated
    population in this repo, is anywhere near them::

        >>> _is_merchandising_slug("camera-lenses")
        True
        >>> _is_merchandising_slug("gift-for-dana-reyes-44-alder-way-portland-97205")
        False
        >>> _is_merchandising_slug("97205")
        False
    """
    if not slug or len(slug) > CATEGORY_SLUG_MAX_CHARS:
        return False
    if len(slug.split("-")) > CATEGORY_SLUG_MAX_TOKENS:
        return False
    return _DIGIT_RUN.search(slug) is None


def _account_category_slugs(account: Mapping[str, Any]) -> set[str]:
    """Every slug :func:`coarsen_categories` can emit for ``account``, admissible or not."""
    slugs: set[str] = set()
    for order in _orders(account):
        category = order.get("category")
        if isinstance(category, str):
            slug = _slug(category)
            if slug:
                slugs.add(slug)
    return slugs


def _identity_fragments_in_slug(slug: str, sources: Iterable[str]) -> set[str]:
    """The distinct identity fragments of ``sources`` that ``slug`` carries.

    Fragments are compared in *slug space* — ``_slug(fragment) in slug`` — rather than
    against the slug's hyphen-separated tokens, and that is the whole point of the function.
    A token-wise comparison sees ``dana-reyes-gear`` and ``danareyes-gear`` as completely
    different strings: the first has two tokens that are identity fragments, the second has
    none, because ``"danareyes"`` is not ``"dana"`` and is not ``"reyes"``. They publish the
    same name. Substring matching in slug space sees both, and it is also what already makes
    ``cookware`` count as *one* collision for a buyer named Cook rather than zero.

    Comparing in slug space is what catches the compound fragments too: the email local part
    ``dana.reyes`` slugs to ``dana-reyes``, so a slug that reassembles it is counted for the
    local part *and* for each of its words, which is three fragments and never a coincidence.

    The returned set is keyed by the *slugged* fragment, so two source values that slug to
    the same string (``"Dana Reyes"`` from ``full_name`` and ``dana.reyes`` from ``email``)
    count once between them. Fragments whose slug is shorter than :data:`_MIN_LEAKABLE` —
    or empty, which punctuation-only values produce and which would otherwise match every
    slug there is — are not counted at all, exactly as they are not leak-checked.
    """
    found: set[str] = set()
    for value in sources:
        folded = _slug(value)
        if len(folded) >= _MIN_LEAKABLE and folded in slug:
            found.add(folded)
    return found


def _exempt_category_slugs(account: Mapping[str, Any]) -> set[str]:
    """This account's own category slugs that a leak report would be wrong about.

    ``category_affinity`` is the one open-vocabulary bucket: its values are slugs of the
    account's own ``orders[].category``. Holding *all* of them out of the haystack is what
    let the buyer who lives on Park Lane keep buying ``park-gear`` — and it is also what let
    an address typed into the category field ride out verbatim while the backstop reported
    clean. So a slug earns the exemption instead of being handed it, on three tests:

    1. it is a plausible merchandising slug at all (:func:`_is_merchandising_slug`) — which
       is what an address, a postal code or a gift note fails;
    2. no token of it *is* an identity fragment contributed by a key that names a person or
       an account (:data:`_NAMING_IDENTITY_KEYS`) — which is what ``dana-reyes`` and
       ``dana-gear`` fail, while ``cookware`` for a buyer named Cook passes, because
       ``"cookware"`` is not ``"cook"``;
    3. it has at least one token that is merchandise rather than the buyer — long enough to
       be leak-checked at all (:data:`_MIN_LEAKABLE`), and not an identity fragment. What
       counts as "an identity fragment" here is the one place the email is treated more
       leniently than everything else, and only for a **one-token** slug:

       * a slug of one token may collide with an email-derived fragment and still be exempt,
         which is what lets ``espresso.fan@example.com`` buy ``espresso``. A one-word
         collision with a self-chosen address is the coincidence the exemption exists for;
       * a slug of several tokens may not. Every token being an identity fragment is how
         ``dana-reyes`` gets assembled out of ``dana.reyes@example.com`` — and an account
         with no name fields at all is exactly what a first magic-link redemption creates,
         so this is the reachable shape, not a hypothetical one. ``park-gear`` still passes,
         on ``"gear"``; ``alder-way-portland`` does not, because ``"way"`` is too short to
         count and the other two tokens are words of the buyer's address.

    4. it carries no more than :data:`_MAX_INCIDENTAL_FRAGMENTS` of this account's identity
       fragments at all (:func:`_identity_fragments_in_slug`). Rule 3 asks whether *some*
       token is merchandise; until T-189 nothing asked how many tokens were the buyer, and
       one innocuous word therefore laundered any amount of identity beside it. On the
       name-less account ``dana-reyes`` is refused by rule 3 and ``dana-reyes-gear`` was
       exempt; ``Alder Way Portland`` is refused and ``alder-way-portland-gear`` was exempt.
       Counting fragments rather than testing for the presence of one merchandise word is
       what closes that, and it closes the run-together spelling (``danareyes-gear``) with
       it, because the count is taken in slug space rather than token by token.

    5. and the *published list* stays inside the same budget, because a disclosure is a
       property of the profile and not of one value in it. ``category_affinity`` carries up
       to :data:`CATEGORY_LIMIT` slugs, so a budget charged per slug is satisfied twice over
       by two orders: ``"dana gear"`` and ``"reyes gear"`` are one collision each and the
       buyer's whole name between them, and ``park-gear`` — the collision this exemption was
       built for — stops being one the moment ``lane-gear`` is published beside it. The
       budget is measured over :func:`coarsen_categories`, which is what a profile actually
       publishes, so a category bought once and truncated away cannot cost the buyer the
       exemption on the ones that survive.

    Rules 2 through 5 are separate on purpose: the first says a name is never a coincidence,
    the second says a slug with nothing but the buyer in it is never one, the third says a
    *pile* of identity words is never one however much merchandise is stacked beside it, and
    the fourth says splitting that pile across several slugs does not make it one either. Any
    one alone leaves a smuggling channel open, and all four together still admit every
    collision the exemption was added for. Being conservative here is close to free: the
    exemption only ever changes an answer for a slug some identity fragment actually matches.
    """
    sources = _identity_sources(account)
    naming = {value for value, keys in sources.items() if keys & _NAMING_IDENTITY_KEYS}

    exempt: set[str] = set()
    for slug in _account_category_slugs(account):
        if not _is_merchandising_slug(slug):
            continue
        tokens = slug.split("-")
        if any(token in naming for token in tokens):
            continue
        # Rule 3's leniency for a ONE-TOKEN slug is "a collision with something the buyer is
        # not" — and that is the judgement `_NAMING_IDENTITY_KEYS` already encodes for rule 2:
        # a *person* is never a coincidence, a *place* routinely is. It used to be spelled
        # `beyond_email`, which admitted `espresso.fan@` buying `espresso` and refused the
        # buyer on **Home** Farm Road buying `home` — a place word, exactly the Park Lane
        # collision this whole function exists to admit. That refusal was invisible until
        # T-199 stopped exempting taxonomy labels wholesale; the blanket exemption had been
        # covering it for the eleven labels, and for nothing else. Naming keys still
        # disqualify, which is what keeps the buyer *surnamed* Home refused — by rule 2, one
        # line above, before this is even reached.
        disqualifying = naming if len(tokens) == 1 else set(sources)
        if not any(len(token) >= _MIN_LEAKABLE and token not in disqualifying for token in tokens):
            continue
        if len(_identity_fragments_in_slug(slug, sources)) > _MAX_INCIDENTAL_FRAGMENTS:
            continue
        exempt.add(slug)

    # Rule 5. The budget is spent by the *release*, not by each slug in it. Charging it per
    # slug is satisfied twice over by two orders, and the profile publishes a list: "dana
    # gear" and "reyes gear" are one collision each and the buyer's whole name between them.
    # Measured over what `coarsen_categories` actually publishes rather than over every slug
    # the account owns, so a category the buyer bought once and that never reaches the
    # profile cannot cost them the exemption on the three that do.
    #
    # Over budget withdraws the exemption entirely, which is exactly "report every fragment
    # the published list carries": a slug that carries none is clean whether it is exempt or
    # not, so nothing else changes answer.
    carried: set[str] = set()
    for slug in coarsen_categories(account):
        if slug in exempt:
            carried |= _identity_fragments_in_slug(slug, sources)
    if len(carried) > _MAX_INCIDENTAL_FRAGMENTS:
        return set()
    return exempt


def identity_leaks(profile: Any, account: Mapping[str, Any]) -> list[str]:
    """Identity values from ``account`` that appear anywhere inside ``profile``.

    Empty means clean. This is the backstop behind the allowlist in :func:`build_buckets`,
    and it refuses things the allowlist alone would not notice — a coarsener rewired to
    return the postal code, an address typed into an order's category field, or a caller who
    passes the buyer's email as the pseudonym::

        >>> account = {"email": "dana.reyes@example.com", "postal_code": "97205"}
        >>> identity_leaks({"pseudonym": "psn-1", "buckets": {"region": "97205"}}, account)
        ['97205']

    Two families of value are held out of the haystack, both **per bucket** and neither
    account-wide (T-139): the fixed vocabulary the bucket's own coarsener emits
    (:data:`_BUCKET_VOCABULARY`), and — in ``category_affinity`` only — the account's own
    category slugs that earn it (:func:`_exempt_category_slugs`). Everything else is matched
    in full, substring and all, **and in slug space as well as verbatim** — see the comment
    on ``slugged_fragments`` below for the phone number that escaped when it was not.
    """
    data = _serialise(profile)
    pseudonym: Any = None
    body: Any = data
    if isinstance(data, Mapping):
        pseudonym = data.get("pseudonym")
        body = {key: value for key, value in data.items() if key != "pseudonym"}

    fragments = _identity_values(account)
    exempt_slugs = _exempt_category_slugs(account)
    # A fragment and the bucket value it has to be found inside are punctuated differently.
    # `_slug` collapses every run of non-alphanumerics to "-", so the phone the account holds
    # as "+1-555-0100" can only ever reach a bucket as `1-555-0100`, and a verbatim search
    # never finds it — while the postal code in the same gift note is found, purely because
    # a postal code carries no punctuation. So every fragment is searched for twice: as
    # written, and in slug space. That is the space `_exempt_category_slugs` already counts
    # fragments in (its rules 4 and 5); this is the same comparison on the matching side.
    slugged_fragments: dict[str, str] = {}
    for value in fragments:
        slugged_value = _slug(value)
        if len(slugged_value) >= _MIN_LEAKABLE and slugged_value != value:
            slugged_fragments[value] = slugged_value
    # Each bucket value is searched on its own. Joining them first made a fragment able to
    # match across the seam between two unrelated values, which is a leak report about a
    # string no bucket ever held.
    own_slugs = _account_category_slugs(account)
    leaked: set[str] = set()
    for bucket, text in _bucket_texts(body):
        folded = text.strip().casefold()
        if folded in _BUCKET_VOCABULARY.get(bucket or "", frozenset()):
            continue
        if bucket == "category_affinity" and (
            folded in exempt_slugs
            # A taxonomy label is incidental exactly when the coarsener really did choose it
            # from the fixed table rather than copying it off the account — which is true of
            # every generalised release and false at rung 0 for a buyer whose own slug spells
            # the label. That distinction is the whole of T-199, and it is narrower than the
            # blanket `_BUCKET_VOCABULARY` entry it replaces in both directions: `last_name`
            # "Home" buying `home` is still reported (the label IS this account's own slug),
            # and a cohort released at k > 1 as `['home']` off orders for `bedding`,
            # `cookware` and `lighting` is still admitted — where the blanket removal failed
            # the ENTIRE release because one buyer's email local part was `home@`.
            or (folded in _TAXONOMY_LABELS and folded not in own_slugs)
        ):
            continue
        # `region`'s alphabet forces a higher floor than the rest — see _MIN_LEAKABLE_BY_BUCKET.
        floor = _MIN_LEAKABLE_BY_BUCKET.get(bucket or "", _MIN_LEAKABLE)
        haystack = text.casefold()
        leaked |= {value for value in fragments if len(value) >= floor and value in haystack}
        slugged_text = _slug(text)
        leaked |= {
            value
            for value, slugged in slugged_fragments.items()
            if len(slugged) >= floor and slugged in slugged_text
        }
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


def _leak_report(account: Mapping[str, Any], leaked: Sequence[str]) -> IdentityLeak:
    """The refusal :func:`build_profile` raises — carrying no identity of its own (T-133).

    The message names how many values escaped and which ``account`` keys they came from. It
    does **not** name the values, and it does not name the session subject either: the
    realistic way this exception is read is a log line or, if a route forgets to catch it, a
    500 traceback, and both of those are places the buyer's address must not be. The subject
    is withheld for the same reason — the one mistake worth catching here is a caller passing
    the email in as the pseudonym, so echoing it discloses exactly when it matters most.
    """
    sources = _identity_sources(account)
    keys = sorted({key for value in leaked for key in sources.get(value, ())})
    return IdentityLeak(
        f"R5: buyer identity reached the store-facing profile — {len(leaked)} value(s) from "
        f"account key(s): {', '.join(keys) or 'unknown'}. The values and the session subject "
        f"are withheld deliberately; naming them here is what made this refusal a disclosure "
        f"path of its own.",
        account_keys=keys,
    )


# --------------------------------------------------------------------------------------
# the public builder
# --------------------------------------------------------------------------------------


def build_profile(
    account: Mapping[str, Any],
    pseudonym: str,
    *,
    k: int | None = None,
    cohort: Sequence[Mapping[str, Any]] = (),
) -> BuyerProfile:
    """The store-facing ``BuyerProfile`` for ``account``, under ``pseudonym``.

    Args:
        account: the buyer's record. May carry every identity field there is; none of them
            are read by a coarsener, and :func:`identity_leaks` proves none of them escaped.
        pseudonym: the pseudonym this session issued — echoed verbatim. It is the *only*
            thing tying the profile to a buyer, and :mod:`buyer_svc.vault` is the only place
            that can undo the tie.
        k: the k-anonymity floor. ``None`` — the default — reads :func:`k_anonymity_floor`,
            which is what puts a deployment's ``PROXYSHOP_BUYER_K_ANONYMITY`` on this path
            without any caller having to pass anything (T-221, T-363).
        cohort: the other accounts released alongside this one, so that a floor which is a
            property of a *release* has a release to be a property of. ``account`` itself may
            appear here and is matched by object identity rather than by value, so it is not
            counted twice — counting a buyer as their own neighbour is exactly how a class of
            one would report as a class of two. Ignored entirely at ``k == 1``.

    What is published at each floor — the failure *direction* being the whole of the ticket:

    * ``k == 1`` — no enforcement, per SPEC §Non-goals. Rung 0: byte for byte what this
      function published before the floor could reach it.
    * ``k > 1`` and the release (``account`` plus ``cohort``) holds at least ``k`` records —
      the rung :func:`_release_levels` assigns this record, which is precisely the rung
      :func:`anonymise_cohort` would have chosen for the same population.
    * ``k > 1`` and the release is smaller than ``k`` — the floor **cannot** be met by any
      generalisation. Every facet is withheld (:data:`UNSATISFIABLE_FLOOR_LEVEL`). Nothing is
      published fine-grained, and there is no fall-through that publishes rung 0 anyway.

    **That last case returns rather than raising, and the asymmetry with**
    :func:`anonymise_cohort` **is deliberate.** The cohort builder raises on the same input
    because it was asked for a *release*, and answering "here is your 5-anonymous release of
    four buyers" would be a lie about a whole population. This function is asked for one
    buyer's profile, on the path ``GET /buyer/profile`` takes; raising there converts the
    knob into a 500 for every buyer in the deployment, which is a denial of service wearing a
    privacy guarantee. Withholding is the same refusal expressed as data instead of an
    exception: the store is told nothing, which is the honest answer when the floor cannot be
    honoured, and the buyer still gets a response.

    Returns:
        ``BuyerProfile(pseudonym=..., buckets=...)`` — top-level keys exactly
        ``{pseudonym, buckets}``, bucket keys exactly :data:`BUCKET_KEYS`.

    Raises:
        ValueError: ``account`` is not a mapping, ``pseudonym`` is empty, or ``k`` is below 1.
        IdentityLeak: an identity value from ``account`` reached the profile. Never
            swallowed — a leaking profile is not returned in a degraded form. Only *this*
            account is checked, never the cohort: a contaminated neighbour must not be able to
            fail every other buyer's profile read, which is what checking the whole release
            here would have done.
    """
    if not isinstance(account, Mapping):
        raise ValueError(f"account must be a mapping, got {type(account).__name__}")
    if not isinstance(pseudonym, str) or not pseudonym.strip():
        raise ValueError("a profile needs the pseudonym its session was issued")

    floor = k_anonymity_floor() if k is None else k
    if floor < 1:
        raise ValueError(f"k must be at least 1, got {floor}")

    if floor == 1:
        buckets = build_buckets(account)
    else:
        release = [account, *(other for other in cohort if other is not account)]
        level = (
            _release_levels(release, floor)[0]
            if len(release) >= floor
            else UNSATISFIABLE_FLOOR_LEVEL
        )
        buckets = buckets_at_level(account, level)

    profile = BuyerProfile(pseudonym=pseudonym, buckets=buckets)

    leaked = identity_leaks(profile, account)
    if leaked:
        raise _leak_report(account, leaked)
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
            raise _leak_report(account, leaked)
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

    ``provenance`` is written as ``'live'`` **explicitly**, on the insert and on the conflict
    branch alike, rather than left to the column default
    (``db/migrations/0005_buyer_accounts_provenance.sql``). The default covers the insert; the
    conflict branch is the case that matters. The window this table feeds
    (:mod:`buyer_svc.window.routes`) labels every row it serves with this column, and a row
    that a real login overwrote while keeping a ``'seed'`` label would be a real buyer shown to
    a store as manufactured — the exact confusion the label exists to prevent, arriving through
    the one path that does not go near the seeder. Saying ``'live'`` here means such a row
    would instead violate ``buyer_accounts_provenance_is_the_key`` and raise: a 500 rather than
    a lie, and a 500 that could only be reached by a vault that had somehow minted a pseudonym
    in the reserved seed namespace, which is a failure worth being loud about.
    """
    dumped = profile.model_dump()
    with connection.cursor() as cur:
        cur.execute(
            "insert into app.buyer_accounts (pseudonym, buckets, provenance) "
            "values (%s, %s::jsonb, 'live') "
            "on conflict (pseudonym) do update set buckets = excluded.buckets, "
            "provenance = 'live'",
            (dumped["pseudonym"], json.dumps(dumped["buckets"], sort_keys=True)),
        )
