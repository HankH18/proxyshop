"""The catalog refresh pipeline — the process that actually **runs** a `CatalogAdapter` (T-236).

Before this module existed, catalog ingestion was a library nobody called. Both adapters were
built and tested, ``mapping.build_upserts`` turned a snapshot into ops, and
``adapters.base.apply_upserts`` replayed ops against Neo4j — and the only non-test
constructions of either adapter in the whole tree were the two ``_satisfies_catalog_adapter``
helpers, which build one purely to run ``isinstance`` against the protocol and throw it away.
A capability the acceptance suite counted as delivered was performed by no running process.

This module is the missing middle. It holds the three things a running ingestion needs and
that neither adapter can supply for itself:

``StoreRegistry``
    *Which* stores exist and where they live. An adapter takes a ``CatalogRequest``; nothing
    in the service knew how to build one, because a ``store_id`` alone does not name a URL.

:func:`build_catalog_adapter`
    *Which* adapter reads a given store. C6 puts both behind one interface precisely so the
    choice is made once, here, and everything downstream holds a bare ``CatalogAdapter``.

``CatalogRefreshRunner``
    The run itself: ``fetch_catalog`` -> ``to_upserts`` -> ``apply_upserts``. That third call
    is the graph write path, which was equally unreached.

Two properties are deliberate.

**A refresh of unchanged content does zero graph work.** The runner keeps the previous run's
``hash_index`` per store and feeds it back as ``known_hashes``. ``build_upserts`` iterates
``snapshot.changed_products``, so a second refresh of a store nothing has touched produces an
empty op list, and the runner writes nothing. ``force=True`` drops the remembered hashes and
makes the next run re-read everything. This is the *observable* half of the differential
guarantee; the durable, cadence-driven scheduler is T-024's and is not built here.

**A missing graph is a warning, not an exception.** ``refresh`` reports what it observed and
what it wrote; a Neo4j that will not answer costs the write and is named in
``report.warnings``, exactly as an adapter reports a refused fetch. A crawl whose results
cannot be stored is still information.

Nothing here is tenant-parameterised and nothing here holds a session between calls: the
session is opened for the write and closed again, so a long-lived service does not hold a
connection open across an idle night.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..adapters.base import (
    CatalogAdapter,
    CatalogRequest,
    CatalogSnapshot,
    UpsertOp,
    apply_upserts,
)
from ..adapters.budgets import CrawlBudget
from ..adapters.catalog_mcp import CatalogMCPAdapter
from ..adapters.hashing import content_hash, snapshot_ref
from ..adapters.mapping import safe_host, safe_split
from ..adapters.netguard import FetchPolicy
from ..adapters.signed_fetch import SignedFetchAdapter

__all__ = [
    "CATALOG_SOURCES",
    "CATALOG_MCP",
    "SIGNED_FETCH",
    "STORES_ENV",
    "CatalogRefreshReport",
    "CatalogRefreshRunner",
    "StoreRegistry",
    "StoreTarget",
    "UnknownStore",
    "build_catalog_adapter",
    "graph_session",
    "observed_now",
    "refresh_store",
]

#: The two ``CatalogAdapter`` implementations C6 names. ``signed_fetch`` reads a storefront
#: over HTTP and is the only one that can see a password-protected dev store (A1);
#: ``catalog_mcp`` reads the catalog MCP server and is the production primary.
SIGNED_FETCH = "signed_fetch"
CATALOG_MCP = "catalog_mcp"
CATALOG_SOURCES: tuple[str, ...] = (SIGNED_FETCH, CATALOG_MCP)

#: Environment variable naming the stores this process may refresh, as a JSON array of
#: :class:`StoreTarget` field mappings. Environment rather than a literal because D41 keeps
#: deployment facts out of the source, and a hard-coded storefront URL in a crawler is the
#: one literal that turns a config mistake into someone else's traffic.
STORES_ENV = "PROXYSHOP_INGEST_STORES"

#: The provenance class every catalog read carries. Both adapters observe the store's own
#: published catalog, so both are ``scraped`` — see ``mapping.catalog_source`` for why
#: classing the MCP read as ``seller_asserted`` would silently unlabel it downstream.
SOURCE_CLASS = "scraped"

#: DESIGN's tie-break rank for a scraped observation. Matches ``extraction.claims``.
AUTHORITY_RANK = 1


class UnknownStore(LookupError):
    """A refresh was asked for a store this process has no target for."""


def observed_now() -> str:
    """The ISO-8601 observation timestamp every run in this package stamps on its facts."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Internal alias kept because every other adapter in this service spells its clock this way.
_now = observed_now


@dataclass(frozen=True)
class StoreTarget:
    """One store this process knows how to refresh.

    Attributes:
        store_id: the graph's stable ID for the store. The ``{store_id}`` in the route.
        base_url: the storefront root, or the shop URL the MCP server is asked about.
        source: which adapter reads it — one of :data:`CATALOG_SOURCES`.
        allowed_hosts: extra hosts a redirect may reach. The base URL's host is always
            allowed; anything else has to be named, which is what stops a store redirecting
            the crawler onto a third party.
        storefront_password: the dev-store password (A1). ``None`` for a public store.
        max_products: stop after this many products even when the budget would allow more.
        fetch_product_pages: follow each product to its HTML page for JSON-LD.
        cassette: for ``catalog_mcp``, a recorded cassette to replay instead of talking to a
            live server. No network, ever, when this is set.
    """

    store_id: str
    base_url: str
    source: str = SIGNED_FETCH
    allowed_hosts: tuple[str, ...] = ()
    storefront_password: str | None = None
    max_products: int = 250
    fetch_product_pages: bool = True
    cassette: str | None = None

    def __post_init__(self) -> None:
        """Refuse a target that cannot produce a fetchable request.

        Raises:
            ValueError: the store id is blank, the source is not one this service
                implements, or the base URL will not parse. Each of the three would fail
                later, inside a crawl, where the cause is far less obvious.
        """
        if not str(self.store_id).strip():
            raise ValueError("StoreTarget.store_id must be non-empty")
        if self.source not in CATALOG_SOURCES:
            raise ValueError(
                f"StoreTarget.source {self.source!r} is not one of {list(CATALOG_SOURCES)}"
            )
        base = str(self.base_url or "").strip()
        if not base or safe_split(base) is None or not safe_host(base):
            raise ValueError(
                f"StoreTarget.base_url {self.base_url!r} does not parse to a host; a target "
                f"whose URL cannot be split names no store"
            )

    def request(
        self,
        *,
        known_hashes: Mapping[str, str] | None = None,
        policy: FetchPolicy | None = None,
        budget: CrawlBudget | None = None,
    ) -> CatalogRequest:
        """The :class:`CatalogRequest` that reads this store under ``policy`` and ``budget``."""
        return CatalogRequest(
            store_id=self.store_id,
            base_url=self.base_url,
            storefront_password=self.storefront_password,
            allowed_hosts=tuple(self.allowed_hosts),
            known_hashes=dict(known_hashes or {}),
            budget=budget or CrawlBudget(),
            policy=policy or FetchPolicy(),
            max_products=int(self.max_products),
            fetch_product_pages=bool(self.fetch_product_pages),
        )


class StoreRegistry:
    """The stores this process may refresh, keyed by ``store_id``.

    Deliberately explicit and deliberately closed: ``POST /refresh/{store_id}`` takes no URL,
    so the only stores a caller can point the crawler at are the ones an operator configured.
    A refresh endpoint that accepted a base URL in its body would be an open SSRF proxy with
    a guard in front of it rather than a catalog refresher.
    """

    def __init__(self, targets: Iterable[StoreTarget] = ()) -> None:
        self._targets: dict[str, StoreTarget] = {}
        for target in targets:
            self.register(target)

    def register(self, target: StoreTarget) -> StoreTarget:
        """Add or replace one target. Returns what is now registered."""
        self._targets[target.store_id] = target
        return target

    def get(self, store_id: str) -> StoreTarget:
        """The target for ``store_id``.

        Raises:
            UnknownStore: nothing is registered under that id.
        """
        try:
            return self._targets[str(store_id)]
        except KeyError:
            raise UnknownStore(
                f"no ingest target registered for store {store_id!r}; known stores are "
                f"{sorted(self._targets)}"
            ) from None

    def __contains__(self, store_id: object) -> bool:
        return str(store_id) in self._targets

    def __len__(self) -> int:
        return len(self._targets)

    @property
    def store_ids(self) -> tuple[str, ...]:
        """Every registered store id, sorted."""
        return tuple(sorted(self._targets))

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, warnings: list[str] | None = None
    ) -> StoreRegistry:
        """Build a registry from :data:`STORES_ENV`, ignoring what it cannot read.

        A malformed entry is skipped and named in ``warnings`` rather than raised: this runs
        at import time inside a route module, and a typo in one store's configuration must
        not take the whole service down with an import error.
        """
        env = os.environ if environ is None else environ
        note = warnings if warnings is not None else []
        raw = str(env.get(STORES_ENV, "") or "").strip()
        if not raw:
            return cls()
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            note.append(f"{STORES_ENV} is not valid JSON ({exc}); no stores registered")
            return cls()
        if not isinstance(payload, list):
            note.append(f"{STORES_ENV} must be a JSON array of objects; no stores registered")
            return cls()

        targets: list[StoreTarget] = []
        known = {f.name for f in StoreTarget.__dataclass_fields__.values()}
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                note.append(f"{STORES_ENV}[{index}] is not an object; skipped")
                continue
            fields = {k: v for k, v in entry.items() if k in known}
            if "allowed_hosts" in fields:
                hosts = fields["allowed_hosts"]
                fields["allowed_hosts"] = tuple(hosts) if isinstance(hosts, list) else ()
            try:
                targets.append(StoreTarget(**fields))
            except (TypeError, ValueError) as exc:
                note.append(f"{STORES_ENV}[{index}] is not a usable store target ({exc}); skipped")
        return cls(targets)


def build_catalog_adapter(
    source: str = SIGNED_FETCH,
    *,
    session: Any = None,
    cassette: str | None = None,
    clock: Any = _now,
) -> CatalogAdapter:
    """Construct the ``CatalogAdapter`` that reads ``source``.

    This is the one place in the service that names a concrete adapter class. C6 puts both
    implementations behind a single interface so that the choice is made exactly once and
    everything downstream — the runner, the route, the graph write — holds a bare
    ``CatalogAdapter`` and cannot tell which it has.

    Args:
        source: one of :data:`CATALOG_SOURCES`.
        session: an MCP client, for ``catalog_mcp`` against a live server.
        cassette: a recorded MCP cassette, replayed instead of a live server. Wins over
            ``session`` because a recording is the deterministic option and a caller that
            supplies one is asking for no network.
        clock: the observation-timestamp source. Injectable so a refresh is reproducible.

    Returns:
        A ``CatalogAdapter``.

    Raises:
        ValueError: ``source`` is not an adapter this service implements.
    """
    if source == SIGNED_FETCH:
        return SignedFetchAdapter(clock=clock)
    if source == CATALOG_MCP:
        if cassette:
            session = CatalogMCPAdapter.from_cassette(cassette).session
        return CatalogMCPAdapter(session=session, clock=clock)
    raise ValueError(f"unknown catalog source {source!r}; expected one of {list(CATALOG_SOURCES)}")


@contextmanager
def graph_session() -> Iterator[Any]:
    """A Neo4j session from the environment's connection details, closed on exit.

    D41: the connection is environment-supplied, never a literal here. Imported lazily so
    that building the app — and therefore importing this module — never needs the driver
    package or a reachable database.
    """
    from ..graph.reembed import graph_driver

    with graph_driver() as driver, driver.session() as session:
        yield session


@dataclass(frozen=True)
class CatalogRefreshReport:
    """What one refresh observed and what it wrote.

    ``ops`` counts the graph writes the snapshot implied and ``written`` names the ids that
    actually landed, so "we had nothing to do" (``ops == 0``) stays distinguishable from
    "we had work and could not do it" (``ops > 0``, ``written == ()``, a warning saying why).
    """

    store_id: str
    base_url: str
    adapter: str
    source: str
    observed_at: str
    job_id: str
    snapshot_ref: str
    extractor_version: str
    products: int = 0
    changed: int = 0
    ops: int = 0
    written: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    hash_index: Mapping[str, str] = field(default_factory=dict)

    @property
    def provenance(self) -> dict[str, Any]:
        """DESIGN ``Provenance`` for every fact this refresh will have written."""
        return {
            "source": SOURCE_CLASS,
            "ref": self.snapshot_ref,
            "observed_at": self.observed_at,
            "authority_rank": AUTHORITY_RANK,
        }


class CatalogRefreshRunner:
    """Run one store's catalog through an adapter and into the graph.

    Args:
        registry: the stores this runner may refresh.
        adapter_factory: how a ``source`` becomes an adapter. Injectable so a test can hand
            in a recorded adapter without an environment variable or a socket.
        session_factory: a context manager yielding a graph session, or ``None`` to compute
            the writes without performing them. Defaults to :func:`graph_session`.
        clock: the observation-timestamp source.
    """

    def __init__(
        self,
        *,
        registry: StoreRegistry | None = None,
        adapter_factory: Any = build_catalog_adapter,
        session_factory: Any = graph_session,
        policy: FetchPolicy | None = None,
        budget: CrawlBudget | None = None,
        clock: Any = _now,
    ) -> None:
        self.registry = registry if registry is not None else StoreRegistry()
        self.adapter_factory = adapter_factory
        self.session_factory = session_factory
        #: The SSRF posture and crawl ceilings every refresh runs under unless the caller
        #: names its own. They live on the runner rather than in the route because
        #: ``POST /refresh`` publishes neither — an operator crawling a dev store on a private
        #: address configures the process, and a request can never widen the posture.
        self.policy = policy
        self.budget = budget
        self._clock = clock
        #: store_id -> the previous run's ``hash_index``. Module-lifetime, per process: the
        #: differential guarantee is a statement about two *runs*, and a ledger that died
        #: with the request could never demonstrate it. T-024 persists this.
        self.hashes: dict[str, dict[str, str]] = {}
        self._runs = 0

    def forget(self, store_id: str) -> None:
        """Drop what was remembered about ``store_id`` so the next refresh re-reads it."""
        self.hashes.pop(str(store_id), None)

    def refresh(
        self,
        store_id: str,
        *,
        force: bool = False,
        policy: FetchPolicy | None = None,
        budget: CrawlBudget | None = None,
        adapter: CatalogAdapter | None = None,
    ) -> CatalogRefreshReport:
        """Read one store's catalog and apply the writes it implies.

        Args:
            store_id: a store registered in :attr:`registry`.
            force: ignore what the previous run hashed, so everything counts as changed.
            policy: the SSRF posture for this crawl, overriding :attr:`policy`. The default
                refuses anything not publicly routable.
            budget: crawl ceilings for this run, overriding :attr:`budget`.
            adapter: use this adapter instead of building one from the target's ``source``.

        Returns:
            A :class:`CatalogRefreshReport`.

        Raises:
            UnknownStore: ``store_id`` is not registered.
        """
        target = self.registry.get(store_id)
        if force:
            self.forget(target.store_id)

        catalog = adapter if adapter is not None else self._adapter_for(target)
        request = target.request(
            known_hashes=self.hashes.get(target.store_id, {}),
            policy=policy or self.policy,
            budget=budget or self.budget,
        )
        snapshot = catalog.fetch_catalog(request)
        ops = catalog.to_upserts(snapshot)

        warnings = list(snapshot.warnings)
        written = self._apply(ops, warnings)
        self.hashes[target.store_id] = dict(snapshot.hash_index)
        self._runs += 1

        return CatalogRefreshReport(
            store_id=target.store_id,
            base_url=snapshot.base_url or target.base_url,
            adapter=snapshot.adapter,
            source=target.source,
            observed_at=snapshot.observed_at,
            job_id=self._job_id(target.store_id, snapshot),
            snapshot_ref=self._snapshot_ref(snapshot),
            extractor_version=snapshot.extractor_version,
            products=len(snapshot.products),
            changed=len(snapshot.changed_products),
            ops=len(ops),
            written=tuple(written),
            warnings=tuple(warnings),
            hash_index=dict(snapshot.hash_index),
        )

    def apply(self, ops: Sequence[UpsertOp], warnings: list[str] | None = None) -> list[str]:
        """Replay ``ops`` against the graph, recording any failure in ``warnings``.

        Public because catalog products are not the only thing a refresh writes: the policy
        pages T-021 extracts produce ``UpsertOp`` records of the same shape, and they must
        reach the graph through the same session handling rather than a second copy of it.
        """
        return self._apply(list(ops), warnings if warnings is not None else [])

    # -- internals ---------------------------------------------------------------------

    def _adapter_for(self, target: StoreTarget) -> CatalogAdapter:
        return self.adapter_factory(
            target.source, cassette=target.cassette, clock=self._clock
        )

    def _apply(self, ops: Sequence[UpsertOp], warnings: list[str]) -> list[str]:
        """Replay ``ops`` against the graph, or say why they were not replayed.

        An empty ``ops`` is the differential guarantee doing its job and must not open a
        connection: a crawl that found nothing changed does no graph work at all, including
        no connection attempt.
        """
        if not ops:
            return []
        if self.session_factory is None:
            warnings.append(
                f"{len(ops)} graph write(s) computed but not applied: no session factory"
            )
            return []
        try:
            with self.session_factory() as session:
                return list(apply_upserts(session, ops))
        except Exception as exc:  # noqa: BLE001 - the driver's failures are not a closed set
            warnings.append(
                f"{len(ops)} graph write(s) computed but not applied: "
                f"{type(exc).__name__}: {exc}"
            )
            return []

    def _job_id(self, store_id: str, snapshot: CatalogSnapshot) -> str:
        """A per-run identifier that is stable for a given store, run index and clock."""
        return f"crawl-{store_id}-{self._runs:04d}-{snapshot.observed_at}"

    @staticmethod
    def _snapshot_ref(snapshot: CatalogSnapshot) -> str:
        """The ref naming *which version* of the store this refresh read.

        The store's own resources carry refs already; this one names the crawl as a whole,
        so a caller holding only the 202 response can still say what the facts came from.
        """
        for resource in snapshot.resources:
            if resource.snapshot_ref:
                return resource.snapshot_ref
        return snapshot_ref(snapshot.base_url, content_hash(snapshot.observed_at))


def refresh_store(
    store_id: str,
    *,
    registry: StoreRegistry | None = None,
    **kwargs: Any,
) -> CatalogRefreshReport:
    """One-shot convenience: refresh ``store_id`` with a fresh runner.

    Useful from a shell or a cron entry. A long-lived service holds a
    :class:`CatalogRefreshRunner` instead, because the differential guarantee lives in the
    hashes the runner remembers between calls.
    """
    runner = CatalogRefreshRunner(registry=registry or StoreRegistry.from_env())
    return runner.refresh(store_id, **kwargs)
