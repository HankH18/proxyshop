"""Field-appropriate refresh cadence — the *configuration* half of T-024.

The R-level requirement is "differential refresh keeps the graph current at **field-appropriate**
cadence". A price moves hourly, a vendor name effectively never, a returns policy a few times a
year. One global refresh interval has to be set for the fastest-moving field, which means every
slow field is re-read hundreds of times for nothing — the crawl budget is spent on the fields that
did not change, and it is a merchant's server paying for it.

So the unit of scheduling here is a **field**, not a store and not a service. Each field declares
how stale it is allowed to get and which *section* — which adapter — has to run to make it fresh
again. :class:`~ingest.scheduler.cycle.RefreshScheduler` unions the sections of the fields that are
overdue, and that union is the set of adapters the tick runs. A tick where only ``offer.price`` is
overdue runs the catalog adapter and **does not fetch a single policy page**.

Where the configuration lives, and why
--------------------------------------
``PROXYSHOP_INGEST_CADENCE`` — the environment, exactly like ``PROXYSHOP_INGEST_STORES`` next door
in :mod:`ingest.scheduler.catalog`, and for the same reason (D41): how often to hit a particular
merchant is a *deployment* fact, not a source fact. The dev store you own can be crawled hourly;
a partner's production storefront cannot, and that difference must not require a code change and a
redeploy of the image.

The value is either an inline JSON array or a path to a file holding one, so an operator with five
fields can use the env var and an operator with fifty can mount a config file and point at it::

    PROXYSHOP_INGEST_CADENCE='[{"field":"offer.price","section":"products","max_age_seconds":900}]'
    PROXYSHOP_INGEST_CADENCE=/etc/proxyshop/cadence.json

:data:`DEFAULT_CADENCE` is what an unconfigured process uses. It is a declarative table rather than
constants threaded through the scheduler, and it is deliberately *not* a config file shipped in the
wheel: a default that lives in a packaged data file is a default that a stale image silently
overrides, and this table is a policy the service owns, not a deployment fact.

**Unusable configuration keeps the defaults.** Unlike :meth:`StoreRegistry.from_env`, which answers
an empty registry when its environment is garbage, a cadence config that parsed to nothing would
mean *no field is ever due*, so the scheduler would run forever and refresh nothing — from outside
identical to a healthy idle service. Falling back to the defaults and warning is loud; silently
freezing the graph is not. Entries that individually fail to parse are skipped and named, so a
partly-good config is honoured as far as it goes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CADENCE_ENV",
    "DEFAULT_CADENCE",
    "POLICIES",
    "PRODUCTS",
    "SECTIONS",
    "CadenceConfig",
    "FieldCadence",
    "sections_of",
]

#: The two surfaces this service can re-read, and therefore the two adapters a cadence entry can
#: schedule. ``products`` is the catalog (``CatalogAdapter``, C6's seam); ``policies`` is the
#: policy-page claim extraction T-021 owns. Defined here rather than in ``routes`` because the
#: cadence table is what *selects* between them and the route module imports this one.
PRODUCTS = "products"
POLICIES = "policies"
SECTIONS: tuple[str, ...] = (PRODUCTS, POLICIES)

#: Environment variable holding the cadence table: an inline JSON array, or a path to a file
#: containing one. See the module docstring for why this is environment rather than source.
CADENCE_ENV = "PROXYSHOP_INGEST_CADENCE"


@dataclass(frozen=True)
class FieldCadence:
    """How stale one field may get, and what has to run to make it fresh.

    Attributes:
        field: the graph field this schedules, ``entity.property`` — ``offer.price``,
            ``product.title``. Free text on purpose: the graph gains properties faster than this
            table can enumerate them, and an operator who wants to schedule one this service has
            never heard of is not making a mistake. What is *not* free text is ``section``.
        section: which surface must be re-read to refresh the field — one of :data:`SECTIONS`.
            This is the load-bearing field: it is what turns a cadence table into a decision about
            which adapter runs.
        max_age_seconds: how old the field's last observation may be before the store is due.
    """

    field: str
    section: str
    max_age_seconds: int

    def __post_init__(self) -> None:
        """Refuse an entry that cannot schedule anything.

        Raises:
            ValueError: the field is unnamed, the section names no adapter, or the interval is
                not a positive whole number of seconds.
        """
        if not str(self.field).strip():
            raise ValueError("FieldCadence.field must be non-empty")
        if self.section not in SECTIONS:
            raise ValueError(
                f"FieldCadence.section {self.section!r} is not one of {list(SECTIONS)}; a cadence "
                f"entry that names no adapter can never make its field fresh"
            )
        try:
            seconds = int(self.max_age_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"FieldCadence.max_age_seconds {self.max_age_seconds!r} is not a whole number"
            ) from exc
        if isinstance(self.max_age_seconds, bool):
            # `int(True) == 1`, so without this `{"max_age_seconds": true}` configures a field
            # that is due every second — a JSON typo turning into a crawl per second at a store.
            raise ValueError("FieldCadence.max_age_seconds must be a number, not a boolean")
        if seconds < 1:
            raise ValueError(
                f"FieldCadence.max_age_seconds must be at least 1, got {seconds}; a "
                f"non-positive interval means the field is permanently overdue"
            )
        object.__setattr__(self, "max_age_seconds", seconds)


#: The cadence an unconfigured process uses. Each interval is a claim about how fast the field
#: actually moves in a storefront, and each ``section`` is the adapter that reads it:
#:
#: * ``offer.price`` / ``offer.availability`` — a merchant repricing or selling out is the whole
#:   reason this graph goes stale; these come from ``/products.json``, the catalog adapter.
#: * ``product.title`` / ``product.vendor`` / ``product.product_type`` — descriptive catalog fields.
#:   Same adapter, one day, because re-reading them hourly buys nothing.
#: * ``policy.claims`` — shipping/returns/warranty claims extracted from policy pages. Weekly:
#:   these are the most expensive surface to re-read (a page fetch and an extraction each) and
#:   the slowest-moving thing the graph holds.
DEFAULT_CADENCE: tuple[FieldCadence, ...] = (
    FieldCadence(field="offer.price", section=PRODUCTS, max_age_seconds=3_600),
    FieldCadence(field="offer.availability", section=PRODUCTS, max_age_seconds=3_600),
    FieldCadence(field="product.title", section=PRODUCTS, max_age_seconds=86_400),
    FieldCadence(field="product.vendor", section=PRODUCTS, max_age_seconds=86_400),
    FieldCadence(field="product.product_type", section=PRODUCTS, max_age_seconds=86_400),
    FieldCadence(field="policy.claims", section=POLICIES, max_age_seconds=604_800),
)


@dataclass(frozen=True)
class CadenceConfig:
    """The whole cadence table this process schedules against."""

    entries: tuple[FieldCadence, ...] = DEFAULT_CADENCE

    def __post_init__(self) -> None:
        """Refuse a table that cannot be scheduled.

        Raises:
            ValueError: the table is empty (nothing would ever be due) or names a field twice
                (two intervals for one field, and nothing says which wins).
        """
        entries = tuple(self.entries)
        if not entries:
            raise ValueError(
                "CadenceConfig needs at least one field; an empty table means no field is ever "
                "due and the scheduler would refresh nothing, silently"
            )
        seen: set[str] = set()
        for entry in entries:
            if entry.field in seen:
                raise ValueError(
                    f"CadenceConfig names {entry.field!r} twice; two intervals for one field "
                    f"leaves nothing to say which one applies"
                )
            seen.add(entry.field)
        object.__setattr__(self, "entries", entries)

    def __iter__(self) -> Iterator[FieldCadence]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def fields(self) -> tuple[str, ...]:
        """Every scheduled field name, in table order."""
        return tuple(entry.field for entry in self.entries)

    @property
    def sections(self) -> tuple[str, ...]:
        """Every section this table can schedule, in :data:`SECTIONS` order."""
        present = {entry.section for entry in self.entries}
        return tuple(section for section in SECTIONS if section in present)

    def for_section(self, section: str) -> tuple[FieldCadence, ...]:
        """The entries whose refresh is performed by ``section``."""
        return tuple(entry for entry in self.entries if entry.section == section)

    @classmethod
    def from_payload(
        cls, payload: object, *, warnings: list[str] | None = None, source: str = CADENCE_ENV
    ) -> CadenceConfig:
        """Build a config from already-decoded JSON, skipping entries it cannot read.

        Args:
            payload: a JSON array of ``{field, section, max_age_seconds}`` objects.
            warnings: appended to, naming everything skipped and why.
            source: what to call the payload in those warnings.

        Returns:
            The configured table, or :data:`DEFAULT_CADENCE` when nothing in ``payload`` was
            usable — see the module docstring for why that is not an empty table.
        """
        note = warnings if warnings is not None else []
        if not isinstance(payload, list):
            note.append(
                f"{source} must be a JSON array of cadence objects; keeping the default cadence"
            )
            return cls()

        known = {"field", "section", "max_age_seconds"}
        entries: list[FieldCadence] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                note.append(f"{source}[{index}] is not an object; skipped")
                continue
            unknown = sorted(set(entry) - known)
            if unknown:
                # Named rather than dropped, for the same reason `StoreRegistry.from_env` names
                # them: `{"secton": "policies"}` would otherwise be a typo that silently schedules
                # nothing and reads as a working config.
                note.append(
                    f"{source}[{index}] has field(s) this service does not read: {unknown}; "
                    f"they were ignored"
                )
            fields = {k: v for k, v in entry.items() if k in known}
            try:
                entries.append(FieldCadence(**fields))
            except (TypeError, ValueError) as exc:
                note.append(f"{source}[{index}] is not a usable cadence entry ({exc}); skipped")

        if not entries:
            note.append(f"{source} produced no usable cadence entries; keeping the default cadence")
            return cls()
        try:
            return cls(tuple(entries))
        except ValueError as exc:
            note.append(f"{source} is not a usable cadence table ({exc}); keeping the default")
            return cls()

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, warnings: list[str] | None = None
    ) -> CadenceConfig:
        """Build a config from :data:`CADENCE_ENV`, or the defaults when it says nothing.

        The value is an inline JSON array when it starts with ``[``, and otherwise a path to a
        file holding one. Both forms are supported because the two deployments differ: a compose
        fragment sets a handful of fields inline, and a real deployment mounts a config file it
        can review in git.

        A missing, unreadable or unparseable value keeps :data:`DEFAULT_CADENCE` and says so in
        ``warnings`` — a scheduler with no cadence refreshes nothing and looks healthy doing it.
        """
        env = os.environ if environ is None else environ
        note = warnings if warnings is not None else []
        raw = str(env.get(CADENCE_ENV, "") or "").strip()
        if not raw:
            return cls()

        source = CADENCE_ENV
        # `{` counts as inline too, though an object is not a valid table: a value that starts
        # with a brace is unambiguously an attempt at JSON, and reporting it as a missing FILE
        # would send an operator looking for a path they never wrote.
        if not raw.startswith(("[", "{")):
            path = Path(raw)
            source = f"{CADENCE_ENV} file {raw}"
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                note.append(f"{source} could not be read ({exc}); keeping the default cadence")
                return cls()
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            note.append(f"{source} is not valid JSON ({exc}); keeping the default cadence")
            return cls()
        return cls.from_payload(payload, warnings=note, source=source)


def sections_of(entries: Sequence[FieldCadence]) -> tuple[str, ...]:
    """The sections ``entries`` require, deduplicated and in :data:`SECTIONS` order.

    Order matters to the caller: a refresh runs products before policies so that a store whose
    catalog crawl unlocks a password-protected dev store (A1) has done so before the policy pages
    are asked for.
    """
    present = {entry.section for entry in entries}
    return tuple(section for section in SECTIONS if section in present)
