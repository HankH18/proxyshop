"""What a load COSTS in round-trips, and the proof that making it cheaper changed no data.

The corpus load is round-trip bound, not CPU bound. MEASURED on the ten recorded storefronts
(3,093 products, one process, one session per store, a pinned clock so the two runs are
comparable) before any of this existed:

    234,119 Cypher statements for 44,803 ops in 333.5 s

and two thirds of those statements said nothing new:

    91,324  (39.0%)  MERGE (s:Source …)      for 3,103 DISTINCT Source nodes
    46,521  (19.9%)  existence probes        for nodes this same batch had just created
    35,040  (15.0%)  MediaAsset + HAS_MEDIA  which nothing in this repository reads

The same corpus through the same path with the memo: **99,377 statements in 174.0 s**, and a
sha256 over every node's and every relationship's properties came back byte-identical.

This file is the regression suite for the two things that removed the first two rows
(:class:`~ingest.graph.upsert.WriteMemo`) and the switch that can remove the third
(:func:`~ingest.adapters.mapping.media_enabled`).

**A performance fix that changes data is a data defect**, so the central test here is not
"it got faster" — it is that the SAME ops replayed with and without the memo produce
byte-identical nodes and relationships. Counting statements is the cheap half; the digest is
the half that grades it.

Run: ``PROXYSHOP_WORKER=<n> ./.venv/bin/python -m pytest
services/ingest/tests/test_load_round_trips.py -q``
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

import pytest
from ingest.adapters import base as adapters_base
from ingest.adapters.base import (
    CatalogSnapshot,
    ImageRecord,
    ProductRecord,
    UpsertOp,
    VariantRecord,
    apply_upserts,
)
from ingest.adapters.mapping import MEDIA_ENV, build_upserts, media_enabled
from ingest.graph import upsert as upsert_module
from ingest.graph.model import Category, MediaAsset, Offer, Product, Source, Store, Variant
from ingest.graph.upsert import ProvenanceRequired

OBSERVED_AT = "2026-01-01T00:00:00+00:00"


def _source(source_id: str = "src-batch", *, confidence: float = 0.9) -> Source:
    """One content-addressed provenance record, as the crawl mints them."""
    return Source(
        source_id=source_id,
        url="https://store.example/products.json",
        content_hash="sha256:" + "0" * 64,
        observed_at=OBSERVED_AT,
        extractor_version="round-trips@1",
        confidence=confidence,
        source_class="scraped",
    )


def _batch(source: Source, *, products: int = 3, images: int = 4) -> list[UpsertOp]:
    """A store, its products, their galleries, variants and offers — one shared ``Source``.

    The shape that matters is the one the real crawl has: every fact of one product names
    the same source, which is why 3,103 distinct sources cost 91,324 merges.
    """
    ops = [
        UpsertOp(kind="store", node=Store(store_id="st-1", domain="store.example"), source=source)
    ]
    for index in range(products):
        product_id = f"prod-{index}"
        ops.append(
            UpsertOp(
                kind="product",
                node=Product(product_id=product_id, canonical_name=f"Product {index}"),
                source=source,
            )
        )
        ops.append(
            UpsertOp(
                kind="sells",
                source=source,
                context={"store_id": "st-1", "product_id": product_id},
            )
        )
        ops.append(
            UpsertOp(
                kind="category",
                node=Category(name="Supplements"),
                source=source,
                context={"product_id": product_id},
            )
        )
        for position in range(1, images + 1):
            ops.append(
                UpsertOp(
                    kind="media",
                    node=MediaAsset(
                        asset_id=f"mda-{index}-{position}",
                        url=f"https://cdn.example/{index}-{position}.jpg",
                        catalogue_hash="sha256:" + "a" * 64,
                        host="cdn.example",
                        on_seller_domain=False,
                        position=position,
                    ),
                    source=source,
                    context={"product_id": product_id},
                )
            )
        variant_id = f"var-{index}"
        ops.append(
            UpsertOp(
                kind="variant",
                node=Variant(variant_id=variant_id, seller_sku=f"SKU-{index}"),
                source=source,
                context={"product_id": product_id},
            )
        )
        ops.append(
            UpsertOp(
                kind="offer",
                node=Offer(
                    offer_id=f"off-{index}",
                    price=19.99 + index,
                    currency="USD",
                    availability="in_stock",
                    observed_at=OBSERVED_AT,
                ),
                source=source,
                context={"store_id": "st-1", "variant_id": variant_id},
            )
        )
    return ops


class CountingSession:
    """A real session with a tally: every statement is forwarded, and counted by shape."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.statements: list[str] = []

    def run(self, statement: str, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(" ".join(str(statement).split()))
        return self.inner.run(statement, *args, **kwargs)

    def count(self, fragment: str) -> int:
        """How many statements carried ``fragment``."""
        return sum(1 for statement in self.statements if fragment in statement)


def _apply_without_memo(session: Any, ops: Sequence[UpsertOp]) -> list[str]:
    """Replay ``ops`` exactly as ``apply_upserts`` did before the memo existed.

    Reaches for the private ``_apply_upserts`` deliberately: the *point* of this test is a
    differential against the un-memoised replay, and the only honest way to have both is to
    call the loop that ``apply_upserts`` now wraps. If this name ever disappears, the memo's
    correctness has lost its control group and this file should fail loudly rather than
    quietly test one path twice.
    """
    return adapters_base._apply_upserts(session, ops, upsert_module)


def _wipe(session: Any) -> None:
    session.run("MATCH (n) DETACH DELETE n").consume()


def _graph_digest(session: Any) -> tuple[str, str, dict[str, int], dict[str, int]]:
    """A content digest of every node and every relationship, plus counts by label and type.

    Counts alone would pass a memo that wrote the right *number* of Source nodes with the
    wrong properties, so the digest covers every property of every node and edge.
    """
    labels = {
        row["label"]: row["c"]
        for row in session.run(
            "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS c ORDER BY label"
        ).data()
    }
    types = {
        row["t"]: row["c"]
        for row in session.run(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY t"
        ).data()
    }
    node_lines = sorted(
        "|".join(sorted(row["labels"]))
        + "#"
        + ";".join(f"{k}={row['props'][k]!r}" for k in sorted(row["props"]))
        for row in session.run("MATCH (n) RETURN labels(n) AS labels, properties(n) AS props")
    )
    rel_lines = sorted(
        f"{row['t']}#{row['props']!r}#{row['a']!r}#{row['b']!r}"
        for row in session.run(
            "MATCH (a)-[r]->(b) RETURN type(r) AS t, properties(r) AS props, "
            "properties(a) AS a, properties(b) AS b"
        )
    )
    node_digest = hashlib.sha256("\n".join(node_lines).encode()).hexdigest()
    rel_digest = hashlib.sha256("\n".join(rel_lines).encode()).hexdigest()
    return node_digest, rel_digest, labels, types


@pytest.mark.docker
@pytest.mark.graph
def test_the_memo_writes_the_same_graph_with_a_third_of_the_statements(
    graph_schema_session: Any,
) -> None:
    """The whole claim, in one test: same ops, same graph, far fewer round-trips.

    Both halves matter and neither is sufficient. A memo that skipped a write it should have
    made would still be fast; a memo that skipped nothing would still be correct. So the
    same batch is replayed twice — once through the un-memoised loop, once through
    ``apply_upserts`` — and the digests of the two resulting graphs must be equal while the
    statement counts must not be.
    """
    session = graph_schema_session
    ops = _batch(_source())

    _wipe(session)
    plain = CountingSession(session)
    _apply_without_memo(plain, ops)
    without = _graph_digest(session)

    _wipe(session)
    memoised = CountingSession(session)
    apply_upserts(memoised, ops)
    with_memo = _graph_digest(session)

    assert with_memo == without, (
        "the memo changed the graph: node/relationship content must be identical"
    )
    assert without[2]["Source"] == with_memo[2]["Source"] == 1

    assert plain.count("MERGE (s:Source") > 1, "the control group must show the redundancy"
    assert memoised.count("MERGE (s:Source") == 1, (
        "one distinct Source in the batch must cost exactly one MERGE"
    )
    assert plain.count("RETURN count(n) AS c") > 0, "the control group must probe"
    assert memoised.count("RETURN count(n) AS c") == 0, (
        "every probed endpoint was created by this same batch, so none needs a probe"
    )
    assert len(memoised.statements) < len(plain.statements) / 2


@pytest.mark.docker
@pytest.mark.graph
def test_a_source_whose_reading_changed_is_written_again(graph_schema_session: Any) -> None:
    """The memo is keyed on the whole node, so a changed property is not swallowed.

    A memo keyed on ``source_id`` alone would treat the second reading as already done and
    leave ``confidence`` at the first value — a performance fix quietly corrupting
    provenance, which is the exact failure this key shape exists to prevent.
    """
    session = graph_schema_session
    _wipe(session)
    first = _source(confidence=0.9)
    second = _source(confidence=0.4)

    counting = CountingSession(session)
    with upsert_module.write_memo(counting):
        upsert_module.upsert_source(counting, first)
        upsert_module.upsert_source(counting, first)
        upsert_module.upsert_source(counting, second)

    assert counting.count("MERGE (s:Source") == 2, "the repeat is elided, the change is not"
    stored = session.run(
        "MATCH (s:Source {source_id: 'src-batch'}) RETURN s.confidence AS confidence"
    ).single()["confidence"]
    assert stored == pytest.approx(0.4), "the second reading must reach the graph"


@pytest.mark.docker
@pytest.mark.graph
def test_a_missing_endpoint_is_still_refused_inside_a_memo(graph_schema_session: Any) -> None:
    """The silent-on-honest-traffic direction: the memo must not become a blanket "yes".

    Only nodes THIS batch created are answered from the memo. An endpoint that genuinely
    does not exist is still probed and still refused — otherwise the memo would convert a
    caught error into an orphaned node, which is what :func:`_require_nodes` exists to stop.
    """
    session = graph_schema_session
    _wipe(session)
    source = _source()

    with upsert_module.write_memo(session), pytest.raises(ProvenanceRequired, match="prod-absent"):
        upsert_module.upsert_media_asset(
            session,
            product_id="prod-absent",
            asset=MediaAsset(
                asset_id="mda-orphan",
                url="https://cdn.example/x.jpg",
                catalogue_hash="sha256:" + "b" * 64,
                position=1,
            ),
            source=source,
        )
    assert session.run("MATCH (m:MediaAsset) RETURN count(m) AS c").single()["c"] == 0


@pytest.mark.docker
@pytest.mark.graph
def test_the_memo_does_not_survive_the_batch_that_built_it(graph_schema_session: Any) -> None:
    """Two ``apply_upserts`` calls do not share a memo, so a wipe between them is honoured.

    This is the property that makes the memo safe against the thing that actually happens in
    this repository: the graph being reset underneath a long-lived process. If a second
    batch could believe the first batch's memo, an emptied database would be re-filled with
    holes where the elided writes used to be.
    """
    session = graph_schema_session
    ops = _batch(_source(), products=1, images=1)

    _wipe(session)
    apply_upserts(session, ops)
    _wipe(session)
    counting = CountingSession(session)
    apply_upserts(counting, ops)

    assert counting.count("MERGE (s:Source") == 1, "the second batch must merge the Source itself"
    assert session.run("MATCH (s:Source) RETURN count(s) AS c").single()["c"] == 1
    assert session.run("MATCH (p:Product) RETURN count(p) AS c").single()["c"] == 1


@pytest.mark.docker
@pytest.mark.graph
def test_a_fact_whose_source_is_absent_is_refused_instead_of_vanishing(
    graph_schema_session: Any,
) -> None:
    """Sabotage: a memo that LIES must produce an error, not a quietly missing product.

    Every material-fact write opens with ``MATCH (src:Source …)``, so a Source that is not
    in the graph made the whole statement match nothing — no node, no ``SUPPORTED_BY`` edge,
    no error — and the load reported success over a graph missing the fact. That was true
    before the memo existed and would have been the memo's failure mode if it were ever
    wrong, which is why the check is on the write rather than on the memo.

    The memo is poisoned by hand here because nothing in the real code can poison it: it
    only ever records writes that returned. That is the point — the guard has to be provoked
    to be observed.
    """
    session = graph_schema_session
    _wipe(session)
    source = _source()

    with upsert_module.write_memo(session) as memo:
        memo.sources.add((source.source_id, tuple(sorted(source.as_properties().items()))))
        with pytest.raises(ProvenanceRequired, match=source.source_id):
            upsert_module.upsert_product(
                session, Product(product_id="prod-ghost", canonical_name="Ghost"), source=source
            )

    assert session.run("MATCH (p:Product) RETURN count(p) AS c").single()["c"] == 0
    assert session.run("MATCH (s:Source) RETURN count(s) AS c").single()["c"] == 0


def test_a_memo_opened_for_another_session_is_ignored() -> None:
    """A memo is trusted only for the session it was populated against.

    Cheap to state, and the reason it is stated: the memo's claim is "this SESSION already
    wrote that", which is worth nothing about a different connection to a possibly different
    database.
    """

    class _Recorder:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def run(self, statement: str, **_: Any) -> Any:
            self.statements.append(statement)

            class _Result:
                def consume(self) -> None:
                    return None

            return _Result()

    owner, stranger = _Recorder(), _Recorder()
    source = _source()
    with upsert_module.write_memo(owner):
        upsert_module.upsert_source(owner, source)
        upsert_module.upsert_source(owner, source)
        upsert_module.upsert_source(stranger, source)
        upsert_module.upsert_source(stranger, source)

    assert len(owner.statements) == 1, "the memo's own session is deduplicated"
    assert len(stranger.statements) == 2, "another session's writes are not"


# =======================================================================================
# The media switch
# =======================================================================================


def _snapshot(*, images: int = 3) -> CatalogSnapshot:
    """A one-product snapshot with a gallery, enough to count media ops."""
    return CatalogSnapshot(
        store_id="st-1",
        base_url="https://store.example",
        observed_at=OBSERVED_AT,
        adapter="signed_fetch",
        extractor_version="round-trips@1",
        products=(
            ProductRecord(
                product_id="prod-0",
                canonical_name="Product 0",
                source_url="https://store.example/products/p0",
                content_hash="sha256:" + "c" * 64,
                categories=("Supplements",),
                variants=(VariantRecord(variant_id="var-0", price=9.99),),
                images=tuple(
                    ImageRecord(
                        native_id=str(position),
                        src=f"https://cdn.example/{position}.jpg",
                        position=position,
                    )
                    for position in range(1, images + 1)
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", True),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("No", False),
        ("OFF", False),
    ],
)
def test_media_is_on_unless_the_environment_explicitly_turns_it_off(
    value: str | None, expected: bool
) -> None:
    """Only the four "off" spellings switch it off; a typo leaves the load as it was.

    An environment variable that turned media off on any unrecognised value would make a
    typo silently drop 17,520 image records from a full load, which is worse than the
    round-trips it saves.
    """
    env = {} if value is None else {MEDIA_ENV: value}
    assert media_enabled(env) is expected


def test_media_ops_are_emitted_by_default_and_only_media_ops_disappear_without_them() -> None:
    """``--no-media`` removes the media ops and nothing else — same ops, same order.

    Asserted as a list difference rather than as two counts, so a change that also dropped a
    variant, reordered the batch or altered a provenance record fails here.
    """
    snapshot = _snapshot(images=3)
    with_media = build_upserts(snapshot, include_media=True)
    without_media = build_upserts(snapshot, include_media=False)

    assert [op.kind for op in with_media].count("media") == 3
    assert [op.kind for op in without_media].count("media") == 0
    assert without_media == [op for op in with_media if op.kind != "media"]
    assert build_upserts(snapshot) == with_media, "the default must be today's behaviour"


def test_the_media_switch_is_actually_reachable_from_the_loader() -> None:
    """Something SETS it: ``--no-media`` on the corpus loader, all the way to the mapping.

    An option nothing sets is the same as an option that does not exist, so this drives the
    real parser and the real adapter rather than asserting the flag's existence.
    """
    from ingest.adapters.signed_fetch import SignedFetchAdapter
    from ingest.scheduler.load_corpus import build_parser

    assert build_parser().parse_args(["--all"]).no_media is False
    assert build_parser().parse_args(["--all", "--no-media"]).no_media is True

    snapshot = _snapshot(images=2)
    assert [op.kind for op in SignedFetchAdapter(include_media=False).to_upserts(snapshot)].count(
        "media"
    ) == 0
    assert [op.kind for op in SignedFetchAdapter().to_upserts(snapshot)].count("media") == 2


def test_the_environment_alone_switches_media_off_on_the_served_crawl_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other setter: ``POST /refresh/{store_id}`` builds its own adapter, so it needs one.

    The corpus loader can pass ``include_media=False`` because a human typed ``--no-media``.
    The scheduler's crawl route builds a ``SignedFetchAdapter`` for itself with no such
    argument, so a deployment that wants media off has only the environment — and if the
    environment did not reach the mapping, the switch would exist for exactly one caller.
    """
    from ingest.adapters.signed_fetch import SignedFetchAdapter

    snapshot = _snapshot(images=2)
    monkeypatch.setenv(MEDIA_ENV, "0")
    assert [op.kind for op in SignedFetchAdapter().to_upserts(snapshot)].count("media") == 0
    monkeypatch.setenv(MEDIA_ENV, "1")
    assert [op.kind for op in SignedFetchAdapter().to_upserts(snapshot)].count("media") == 2
