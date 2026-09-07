"""Replay a **recorded** storefront crawl through the live crawl path, opening no socket.

WHY THIS EXISTS. ``fixtures/real-catalogs/`` holds ten real supplement storefronts recorded
whole on 2026-09-07 — 3,093 products, verbatim bytes, with row-aligned provenance (source
URL, HTTP status, fetch instant, page digest, byte span, per-record digest). The crawl path
worked and landed provenanced nodes. **Nothing had ever loaded one into the other**, so every
graph demo in this repository ran on four families of generated coffee.

The obvious way to close that — write a "corpus importer" that builds ``Product`` nodes from
the JSONL — is the wrong one, and it is wrong in a way this repository has been bitten by
before: it would be a *second* ingestion path. Its provenance would be minted by different
code, its ids by different code, its change detection by different code, and the graph the
demo runs on would then be a graph no crawl can produce. What "real data in the graph" is
worth depends entirely on it having arrived the way real data arrives.

So this module replaces the **transport** and nothing else. ``SignedFetchAdapter`` takes a
pre-built ``client``; hand it a :class:`RecordedTransport` and every subsequent step is the
live one — the same robots posture, the same pagination loop, the same ``products.json``
parse, the same ``native_product_key`` refusal, the same ``composite_hash`` change
detection, the same ``build_upserts``, the same ``apply_upserts``, the same provenance.
The only thing that differs is where the bytes come from.

THE BYTES ARE THE ONES THE STOREFRONT SERVED, and that is checkable rather than asserted.
The recorded provenance carries ``byte_span`` per product and ``response_sha256`` for the
whole page, so a page is reassembled as ``{"products":[`` + the verbatim record bytes joined
by ``,`` + ``]}`` and its digest compared against what the live fetch recorded. All 18 pages
of the corpus reproduce their recorded digest exactly. A page that does not is
:class:`CorpusIntegrityError` — a corrupted corpus must not quietly become graph rows.

WHAT IS SYNTHESISED, said plainly. The collector recorded each host's robots.txt *decision*
(status, ``products_json_allowed``, ``crawl_delay``) and its digest, but **not its body**.
The replay therefore renders a robots.txt that reproduces the recorded decision, and
:meth:`RecordedStore.robots_body` is checked against ``products_json_allowed`` when the
corpus is loaded, so a host the real crawl was forbidden stays forbidden here. The body is
labelled ``X-ProxyShop-Replay`` on the response and its digest is deliberately *not* the
recorded robots digest, because it is not the recorded bytes.

NO SOCKET, STRUCTURALLY. This module imports no networking library at all — no ``socket``,
no ``http.client``, no ``ssl``. There is nothing here to point at a host, which is a stronger
guarantee than a policy that could be widened by a flag. D3/C9: the suite runs offline, and
these are other people's servers.
"""

from __future__ import annotations

import gzip
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .budgets import CrawlLedger
from .hashing import HASH_PREFIX, content_hash, snapshot_ref
from .netguard import host_matches_allowlist, normalise_host, safe_split
from .robots import USER_AGENT, may_fetch
from .transport import HTTPResult, TransportError

__all__ = [
    "CORPUS_ENV",
    "DEFAULT_CORPUS_DIR",
    "PRODUCTS_PATH",
    "CorpusIntegrityError",
    "RecordedCorpus",
    "RecordedPage",
    "RecordedStore",
    "RecordedTransport",
    "UnrecordedRequest",
    "default_corpus_dir",
]

#: Environment override for where the recorded corpus lives. A path rather than a literal
#: (D41), and read only as a default — every entry point takes the directory explicitly so a
#: test never depends on the environment of the process running it.
CORPUS_ENV = "PROXYSHOP_RECORDED_CATALOGS"

#: The corpus directory as it sits in this repository, relative to the repo root.
DEFAULT_CORPUS_DIR = Path("fixtures") / "real-catalogs"

#: The catalogue surface the corpus recorded. The only path a replay serves besides robots.
PRODUCTS_PATH = "/products.json"

#: What the collector paginated with, and therefore the only page size the recording can
#: answer. ``SignedFetchAdapter`` asks for ``min(250, max_products)``, so a caller that sets
#: ``max_products`` below this would ask for a page that was never recorded.
RECORDED_PAGE_SIZE = 250


class CorpusIntegrityError(RuntimeError):
    """The corpus on disk does not reproduce what the live fetch recorded.

    Raised at load time, never at fetch time: a crawl that has already started must not
    discover halfway through that its bytes are not the ones that were served.
    """


class UnrecordedRequest(TransportError):
    """The replay was asked for a URL the recording does not contain.

    A subclass of :class:`~ingest.adapters.transport.TransportError` so the adapter treats it
    exactly as it treats a live transport failure — a warning on the snapshot, not an
    exception out of ``fetch_catalog``. It is *never* a fallback to the network: there is no
    network here.
    """


def default_corpus_dir() -> Path:
    """Where the recorded corpus lives, from the environment or from the repo layout."""
    override = str(os.environ.get(CORPUS_ENV, "") or "").strip()
    if override:
        return Path(override)
    # services/ingest/src/adapters/recorded.py -> adapters -> src -> ingest -> services -> root
    return Path(__file__).resolve().parents[4] / DEFAULT_CORPUS_DIR


@dataclass(frozen=True)
class RecordedPage:
    """One recorded ``products.json`` page, reassembled from the records it carried.

    Attributes:
        number: the ``page`` query value the collector fetched it with.
        url: the URL the collector fetched, verbatim.
        status: the HTTP status the storefront answered with.
        fetched_at: when.
        body: the page bytes, reassembled from the verbatim per-product byte slices.
        recorded_digest: the ``sha256`` the live fetch took over the whole response.
        products: how many product records the page carried.
    """

    number: int
    url: str
    status: int
    fetched_at: str
    body: bytes
    recorded_digest: str
    products: int

    @property
    def reproduces_recorded_bytes(self) -> bool:
        """Whether the reassembled page hashes to what the live fetch recorded."""
        return content_hash(self.body).removeprefix(HASH_PREFIX) == self.recorded_digest


@dataclass(frozen=True)
class RecordedStore:
    """One store's slice of the recording, ready to be replayed."""

    host: str
    origin: str
    role: str
    robots_status: int
    products_json_allowed: bool
    crawl_delay_seconds: float
    pages: tuple[RecordedPage, ...]
    products_recorded: int
    truncated: bool = False

    @property
    def store_id(self) -> str:
        """The graph store id for this host — the host itself, which is already stable."""
        return self.host

    @property
    def robots_body(self) -> str:
        """A robots.txt reproducing the recorded DECISION. Not the recorded bytes.

        The collector stored ``products_json_allowed`` and ``crawl_delay``, not the file. A
        replay still has to answer robots, because the crawl path fetches it first and
        obeys it — skipping that would replay a crawl the real one might not have been
        allowed to make. So the decision is rendered back into the grammar the crawl parses,
        and :meth:`RecordedCorpus.integrity_problems` checks the rendering really does
        reproduce the recorded verdict.
        """
        rule = "Allow: /products.json" if self.products_json_allowed else "Disallow: /products.json"
        delay = f"Crawl-delay: {self.crawl_delay_seconds:g}\n" if self.crawl_delay_seconds else ""
        return (
            "# Rendered by ingest.adapters.recorded from the RECORDED DECISION for "
            f"{self.host}. This is not the bytes the host served.\n"
            f"User-agent: *\n{rule}\n{delay}"
        )

    def page(self, number: int) -> RecordedPage | None:
        """The recorded page numbered ``number``, or ``None`` when it was never fetched."""
        return next((page for page in self.pages if page.number == number), None)


@dataclass(frozen=True)
class RecordedCorpus:
    """Every recorded store, keyed by host."""

    root: Path
    collected_at: str
    corpus_version: str
    stores: tuple[RecordedStore, ...] = ()
    skipped: tuple[str, ...] = ()

    @classmethod
    def load(cls, root: Path | str | None = None, *, hosts: Sequence[str] = ()) -> RecordedCorpus:
        """Read the corpus off disk and reassemble every recorded page.

        Args:
            root: the corpus directory. Defaults to :func:`default_corpus_dir`.
            hosts: load only these hosts. Empty means all of them.

        Returns:
            The loaded corpus.

        Raises:
            FileNotFoundError: the directory or its ``collection.json`` is not there.
            CorpusIntegrityError: a reassembled page does not reproduce the digest the live
                fetch recorded, or a rendered robots.txt does not reproduce the recorded
                verdict.
        """
        base = Path(root) if root is not None else default_corpus_dir()
        manifest_path = base / "collection.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"no recorded catalogue corpus at {base} (expected {manifest_path}); set "
                f"{CORPUS_ENV} or pass the directory explicitly"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wanted = {str(h).strip().lower() for h in hosts if str(h).strip()}

        stores: list[RecordedStore] = []
        skipped: list[str] = []
        for entry in manifest.get("stores") or []:
            host = str(entry.get("host") or "").strip().lower()
            if not host or (wanted and host not in wanted):
                continue
            if entry.get("skipped") is not None:
                skipped.append(host)
                continue
            stores.append(_load_store(base, entry))

        corpus = cls(
            root=base,
            collected_at=str(manifest.get("collected_at") or ""),
            corpus_version=str(manifest.get("corpus_version") or ""),
            stores=tuple(stores),
            skipped=tuple(skipped),
        )
        problems = corpus.integrity_problems()
        if problems:
            raise CorpusIntegrityError(
                f"{len(problems)} recorded page(s) do not reproduce what was served:\n  "
                + "\n  ".join(problems)
            )
        return corpus

    def by_host(self, host: str) -> RecordedStore:
        """The recorded store for ``host``.

        Raises:
            KeyError: nothing was recorded for that host.
        """
        key = str(host).strip().lower()
        for store in self.stores:
            if store.host == key:
                return store
        raise KeyError(f"{host!r} is not in the recorded corpus at {self.root}")

    @property
    def hosts(self) -> tuple[str, ...]:
        """Every recorded host, in manifest order."""
        return tuple(store.host for store in self.stores)

    @property
    def products_recorded(self) -> int:
        """How many products the recording holds across every loaded store."""
        return sum(store.products_recorded for store in self.stores)

    def integrity_problems(self) -> list[str]:
        """Everything about this corpus that would make a replay a lie.

        Two checks, and both are about the replay being the recording rather than something
        shaped like it:

        * every reassembled page hashes to the digest the *live* fetch took over the whole
          response, so the adapter is parsing the bytes the storefront actually served;
        * every rendered robots.txt yields the verdict the collector recorded, so a host the
          crawl was forbidden is still forbidden on replay.
        """
        problems: list[str] = []
        for store in self.stores:
            for page in store.pages:
                if not page.reproduces_recorded_bytes:
                    problems.append(
                        f"{store.host} page {page.number}: reassembled {len(page.body)} bytes "
                        f"hash to {content_hash(page.body)}, recorded "
                        f"{HASH_PREFIX}{page.recorded_digest}"
                    )
            products_url = f"{store.origin.rstrip('/')}{PRODUCTS_PATH}"
            replayed = may_fetch(store.robots_body, products_url, USER_AGENT)
            if replayed != store.products_json_allowed:
                problems.append(
                    f"{store.host}: rendered robots.txt answers {replayed} for "
                    f"{products_url}, recorded decision was {store.products_json_allowed}"
                )
        return problems


def _load_store(base: Path, entry: Mapping[str, Any]) -> RecordedStore:
    """Reassemble one manifest entry's pages from its products and provenance files."""
    host = str(entry["host"]).strip().lower()
    files = entry.get("files") or {}
    raw_products = _read_lines(base / str(files.get("products", "")))
    provenance = [json.loads(line) for line in _read_lines(base / str(files.get("provenance", "")))]
    if len(raw_products) != len(provenance):
        raise CorpusIntegrityError(
            f"{host}: {len(raw_products)} product record(s) but {len(provenance)} provenance "
            f"row(s); the two files are row-aligned by construction"
        )

    grouped: dict[int, list[bytes]] = {}
    meta: dict[int, Mapping[str, Any]] = {}
    for raw, prov in zip(raw_products, provenance, strict=True):
        number = int(prov.get("page") or 1)
        grouped.setdefault(number, []).append(raw)
        meta.setdefault(number, prov)

    pages = tuple(
        RecordedPage(
            number=number,
            url=str(meta[number].get("source_url") or ""),
            status=int(meta[number].get("http_status") or 200),
            fetched_at=str(meta[number].get("fetched_at") or ""),
            # The exact shape `products.json` serves and the exact shape the byte spans were
            # taken from: an object with one `products` array, records verbatim and
            # comma-separated. Reproducing the recorded page digest is what proves it.
            body=b'{"products":[' + b",".join(records) + b"]}",
            recorded_digest=str(meta[number].get("response_sha256") or ""),
            products=len(records),
        )
        for number, records in sorted(grouped.items())
    )
    robots = entry.get("robots") or {}
    return RecordedStore(
        host=host,
        origin=str(entry.get("effective_origin") or f"https://{host}"),
        role=str(entry.get("role") or ""),
        robots_status=int(robots.get("status") or 0),
        products_json_allowed=bool(robots.get("products_json_allowed")),
        crawl_delay_seconds=float(robots.get("crawl_delay_seconds") or 0.0),
        pages=pages,
        products_recorded=int(entry.get("products_recorded") or len(raw_products)),
        truncated=bool(entry.get("truncated")),
    )


def _read_lines(path: Path) -> list[bytes]:
    """Every non-empty line of a ``.jsonl`` or ``.jsonl.gz`` file, as verbatim bytes.

    Decompression is not re-serialisation — gzip restores the original octets exactly — so
    the digests survive the corpus being stored compressed. Nothing here parses and re-emits
    a record; that would destroy the byte-exactness the whole replay rests on.
    """
    if not path.is_file():
        raise FileNotFoundError(f"recorded corpus file missing: {path}")
    blob = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return [line for line in blob.split(b"\n") if line.strip()]


@dataclass
class RecordedTransport:
    """A ``SafeHTTPClient``-shaped replay of one recorded store. Opens nothing.

    Duck-typed rather than a subclass on purpose: subclassing the real transport would
    inherit ``_open``, and a replay that *could* connect is a replay that eventually does.
    This class has no code path to a socket because it imports no networking library.

    The budget is still charged — pages, wire bytes, decompressed bytes and the clock — so a
    replay is subject to the same :class:`~ingest.adapters.budgets.CrawlBudget` a live crawl
    is, and a corpus larger than the budget fails the same way a large storefront does
    rather than sailing past a ceiling the real path enforces.

    Attributes:
        store: the recorded store to answer for.
        requested: every URL this transport was asked for, in order. The evidence that a
            replay really did drive the pagination loop rather than being handed a list.
    """

    store: RecordedStore
    user_agent: str = USER_AGENT
    requested: list[str] = field(default_factory=list)

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        allowed_hosts: Sequence[str] = (),
        ledger: CrawlLedger | None = None,
    ) -> HTTPResult:
        """Answer ``url`` from the recording, or refuse.

        Args:
            url: absolute URL, as the adapter built it.
            method: HTTP method. Anything but ``GET`` is refused: the recording is of a
                catalogue read, and a storefront-password POST was never part of it.
            body: ignored; present so the signature matches the live transport.
            headers: ignored, likewise.
            allowed_hosts: enforced exactly as the live transport enforces it, so a replay
                cannot answer for a host the crawl was not allowed to touch.
            ledger: the crawl's budget ledger; charged as a live fetch charges it.

        Returns:
            The recorded response.

        Raises:
            UnrecordedRequest: the recording has no answer for this URL.
        """
        book = ledger or CrawlLedger()
        self.requested.append(url)
        split = safe_split(url)
        if split is None:
            raise UnrecordedRequest(f"{url}: does not parse")
        host = normalise_host(split.hostname or "")
        allow = list(allowed_hosts) if allowed_hosts else [host]
        if not host_matches_allowlist(host, allow):
            raise UnrecordedRequest(f"{url}: host-not-allow-listed:{host}")
        if method.upper() != "GET":
            raise UnrecordedRequest(f"{url}: the recording holds only GET responses")

        book.check_time()
        book.charge_page()
        path = split.path or "/"
        if path == "/robots.txt":
            return self._respond(
                url, 200, self.store.robots_body.encode("utf-8"), "text/plain", book
            )
        if path == PRODUCTS_PATH:
            return self._products(url, split.query, book)
        raise UnrecordedRequest(
            f"{url}: the recorded corpus holds {PRODUCTS_PATH} and /robots.txt only; it "
            f"carries no product pages, so nothing here can invent one"
        )

    # -- internals -------------------------------------------------------------------------

    def _products(self, url: str, query: str, book: CrawlLedger) -> HTTPResult:
        """One recorded catalogue page, or an empty page past the end of the recording."""
        params = parse_qs(query or "")
        try:
            number = int((params.get("page") or ["1"])[0])
            limit = int((params.get("limit") or [str(RECORDED_PAGE_SIZE)])[0])
        except (TypeError, ValueError) as exc:
            raise UnrecordedRequest(f"{url}: unreadable pagination ({exc})") from exc
        if limit != RECORDED_PAGE_SIZE:
            # The collector paginated at 250 and the byte spans are of THOSE pages. A caller
            # asking for a different page size is asking for a response that was never
            # served, and answering it with a slice of a 250-page would be a fabrication
            # wearing a recorded digest.
            raise UnrecordedRequest(
                f"{url}: the corpus was recorded at limit={RECORDED_PAGE_SIZE}; a crawl of it "
                f"must not narrow max_products below that"
            )
        page = self.store.page(number)
        if page is None:
            # Past the end of what was recorded. An empty `products` array is exactly what a
            # storefront answers past its last page, and it is what ends the adapter's
            # pagination loop — so the replay ends the same way the live crawl did.
            return self._respond(url, 200, b'{"products":[]}', "application/json", book)
        return self._respond(url, page.status, page.body, "application/json", book)

    def _respond(
        self, url: str, status: int, payload: bytes, media_type: str, book: CrawlLedger
    ) -> HTTPResult:
        """Meter a recorded body through the budget and shape it as the live transport does."""
        total = len(payload)
        book.charge_bytes(total, response_total=total)
        book.charge_decompressed(total, response_total=total)
        digest = content_hash(payload)
        return HTTPResult(
            url=url,
            final_url=url,
            status=status,
            headers={
                "content-type": f"{media_type}; charset=utf-8",
                "content-length": str(total),
                # Loud on the wire, not only in a docstring: anything reading this response
                # can tell it came out of a recording rather than off a storefront.
                "x-proxyshop-replay": f"recorded-corpus:{self.store.host}",
            },
            body=payload,
            redirect_chain=(url,),
            content_hash=digest,
            snapshot_ref=snapshot_ref(url, digest),
            bytes_downloaded=total,
            connected_to="",
        )


def recorded_transports(
    corpus: RecordedCorpus,
) -> Iterator[tuple[RecordedStore, RecordedTransport]]:
    """Every recorded store paired with a fresh transport for it."""
    for store in corpus.stores:
        yield store, RecordedTransport(store=store)
