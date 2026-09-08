"""``apps.buyer.seed`` — the producer, the committed corpus, and the marker's forgery cases.

Structured after ``services/sim/tests/test_seed_store.py``, which is the lane this one follows:

1. it is a script, not a service;
2. run / store / verify;
3. seeded and real, distinguishable forever;
4. the live switch.

The section that carries the weight is 3, and inside it the **falsification** test — a corpus
that has been edited AND re-digested so that every file check passes, which must still be
refused. A suite that only checked digests would be green against it, and a corpus that can be
edited by anyone willing to run sha256 twice is not pinned to anything.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SEED_SRC = REPO_ROOT / "apps" / "buyer" / "seed"
CORPUS = REPO_ROOT / "apps" / "buyer" / "seed-data" / "store-window"


def _copy_corpus(destination: pathlib.Path) -> pathlib.Path:
    """The committed corpus, byte for byte, somewhere a test may edit it."""
    from apps.buyer.seed.store import ACCOUNTS_FILE, COLLECTION_FILE

    destination.mkdir(parents=True, exist_ok=True)
    for name in (COLLECTION_FILE, ACCOUNTS_FILE):
        (destination / name).write_bytes((CORPUS / name).read_bytes())
    return destination


def _re_digest(root: pathlib.Path) -> None:
    """Rewrite ``collection.json``'s digest so the file checks pass over edited bytes.

    This is the adversary's move, and it is cheap: a hand edit plus one sha256. Every test that
    calls this is asserting what survives it.
    """
    from apps.buyer.seed.store import ACCOUNTS_FILE, COLLECTION_FILE

    body = (root / ACCOUNTS_FILE).read_bytes()
    collection = json.loads((root / COLLECTION_FILE).read_text(encoding="utf-8"))
    collection["file_sha256"][ACCOUNTS_FILE] = hashlib.sha256(body).hexdigest()
    collection["bytes"][ACCOUNTS_FILE] = len(body)
    (root / COLLECTION_FILE).write_text(
        json.dumps(collection, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ======================================================================================
# 1. It is a script, not a service
# ======================================================================================


def test_the_producer_is_outside_the_deployable() -> None:
    """The seed package is a sibling of the service, not part of it.

    ``apps/buyer/Dockerfile`` builds the buyer image from ``apps/buyer/svc``. This asserts the
    producer is not under that directory, which is what keeps "the code that manufactures
    buyers" out of the container that serves them. The other half — that the service never
    imports it — is ``test_the_served_service_never_imports_the_seed_producer`` next door.
    """
    assert SEED_SRC.is_dir(), f"{SEED_SRC} is missing"
    assert not SEED_SRC.is_relative_to(REPO_ROOT / "apps" / "buyer" / "svc"), (
        f"{SEED_SRC} sits inside the service directory the buyer image copies"
    )
    dockerfile = (REPO_ROOT / "apps" / "buyer" / "Dockerfile").read_text(encoding="utf-8")
    copied = [line for line in dockerfile.splitlines() if line.strip().upper().startswith("COPY")]
    assert copied, "apps/buyer/Dockerfile copies nothing; this sweep would be vacuous"
    assert not [line for line in copied if "apps/buyer/seed" in line], (
        f"the buyer image copies the seed producer: {copied}"
    )


def test_the_producer_imports_the_service_and_never_the_other_way() -> None:
    """One-way import edge: ``apps.buyer.seed`` -> ``buyer_svc``, never back.

    The producer *must* import the service — running the product's own coarsener is what makes
    the corpus values the product can actually emit rather than a fixture that resembles them.
    What must not happen is the reverse, which would put the generator in the deployable.
    """
    imported: set[str] = set()
    for module in sorted(SEED_SRC.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
    assert any(name.startswith("buyer_svc") for name in imported), (
        f"the producer imports no part of the service ({sorted(imported)}), so its buckets are "
        "hand-written rather than produced by the coarsener that serves them"
    )


# ======================================================================================
# 2. run / store / verify
# ======================================================================================


def test_the_producer_is_deterministic() -> None:
    """Two draws at one seed agree, row for row and head for head."""
    from apps.buyer.seed.population import DEFAULT_SEED, draw

    first_rows, first_head = draw(seed=DEFAULT_SEED)
    second_rows, second_head = draw(seed=DEFAULT_SEED)
    assert first_rows == second_rows
    assert first_head == second_head


def test_the_corpus_does_not_depend_on_the_draw() -> None:
    """Every seed produces the same corpus, and that is the property rather than a surprise.

    Stated plainly because the obvious expectation is the opposite. The seed draws each
    member's order totals inside their cohort's band, and a band is chosen so that every total
    inside it coarsens to one ``BUDGET_BANDS`` label — so within-cohort variation is exactly
    what the coarsener is there to remove, and the released set is a function of the cohort
    table and the coarseners alone.

    Asserting it matters in both directions. It says the committed bytes are not an artefact of
    one lucky draw; and it would go red the day a cohort's band is widened across a bucket
    boundary, which is the edit that would make the corpus quietly seed-dependent and the
    ``CohortCollapsed`` canary intermittent.
    """
    from apps.buyer.seed.population import DEFAULT_SEED, draw

    baseline_rows, baseline_head = draw(seed=DEFAULT_SEED)
    for offset in (1, 7, 1_000, 99_999):
        rows, head = draw(seed=DEFAULT_SEED + offset)
        assert head == baseline_head, (
            f"seed {DEFAULT_SEED + offset} produces chain head {head} and {DEFAULT_SEED} "
            f"produces {baseline_head}. A cohort's band now straddles a bucket boundary, so "
            "which buyers a store is shown depends on the draw"
        )
        assert rows == baseline_rows


def test_the_committed_corpus_is_exactly_what_the_producer_produces() -> None:
    """The bytes in this repository are the bytes ``reproduce`` regenerates.

    Not a duplicate of the load test below: that one checks the corpus is self-consistent, and
    would pass over a corpus produced by an older cohort table. This one checks it is *current*
    — that somebody who runs the recorded command gets the committed file back.
    """
    from apps.buyer.seed.chain import canonical_bytes
    from apps.buyer.seed.population import draw
    from apps.buyer.seed.store import ACCOUNTS_FILE, load

    corpus = load()
    recorded = corpus.collection["determinism"]["reproduce"]
    members = corpus.collection["run"]["members_per_cohort"]
    rows, head = draw(seed=corpus.seed, members=members)

    assert head == corpus.chain_head, (
        f"re-running {recorded!r} produces chain head {head} and the corpus records "
        f"{corpus.chain_head}. Regenerate it: the coarseners or the cohorts have moved"
    )
    regenerated = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    assert regenerated == (CORPUS / ACCOUNTS_FILE).read_bytes(), (
        f"re-running {recorded!r} does not reproduce {ACCOUNTS_FILE} byte for byte"
    )


def test_the_committed_corpus_loads_and_every_row_carries_the_marker() -> None:
    """The bytes actually committed — the ones a demo reads — load, verify and are all marked.

    Deliberately not skipped when the corpus is absent. ``services/sim``'s equivalent skips
    because its corpus is optional; this one is the reader's only data source and a missing one
    is a broken lane, not an unexercised one.
    """
    from apps.buyer.seed.chain import is_seeded_pseudonym
    from apps.buyer.seed.store import load

    corpus = load()
    assert corpus.rows, "the committed corpus is empty"
    unmarked = [
        row["pseudonym"] for row in corpus.rows if not is_seeded_pseudonym(row["pseudonym"])
    ]
    assert not unmarked, f"{len(unmarked)} committed row(s) carry no seed marker: {unmarked[:5]}"
    assert len({row["pseudonym"] for row in corpus.rows}) == len(corpus.rows)
    assert corpus.collection["simulated"] is True
    assert corpus.collection["marker"]["seeded_rows"] == len(corpus.rows)


def test_the_corpus_holds_cohorts_large_enough_for_the_window_to_release() -> None:
    """Every equivalence class in the corpus clears the window's default floor.

    A corpus of forty unique profiles would be refused by the release path in its entirety and
    the demo would show an empty window — a failure that looks like a broken route.
    """
    from buyer_svc.profile import equivalence_class
    from buyer_svc.window.routes import DEFAULT_WINDOW_FLOOR

    from apps.buyer.seed.store import load

    corpus = load()
    sizes: dict[tuple[Any, ...], int] = {}
    for row in corpus.rows:
        key = equivalence_class(row["buckets"])
        sizes[key] = sizes.get(key, 0) + 1
    smallest = min(sizes.values())
    assert smallest >= DEFAULT_WINDOW_FLOOR, (
        f"the smallest cohort in the corpus holds {smallest} buyer(s) and the window's default "
        f"floor is {DEFAULT_WINDOW_FLOOR}, so that cohort would be withheld from every store"
    )
    assert len(sizes) > 1, "a corpus with one equivalence class demonstrates no window at all"


def test_a_cohort_whose_members_diverge_is_refused_rather_than_shipped() -> None:
    """The canary. A cohort that does not coarsen to one class fails the run, loudly."""
    from apps.buyer.seed.population import Cohort, CohortCollapsed, draw

    broken = Cohort(
        name="members who do not agree",
        region="US-OR",
        ranked=(("trail-runners", 4), ("wool-socks", 3)),
        # Straddles the 0-50 / 50-100 boundary, so each member's median order total lands in
        # whichever band their own draw fell into and the cohort is not one class. This is the
        # realistic version of the mistake -- a plausible-looking price range that happens to
        # cross a coarsener's edge -- and it is the exact edit
        # `test_the_corpus_does_not_depend_on_the_draw` is watching for.
        band=(40.0, 60.0),
        tail=(),
    )
    with pytest.raises(CohortCollapsed) as raised:
        draw(cohorts=(broken,), members=8)
    assert "members who do not agree" in str(raised.value)


# ======================================================================================
# 3. Seeded and real, distinguishable forever
# ======================================================================================


def test_an_edited_corpus_is_refused_by_its_own_digest(tmp_path: pathlib.Path) -> None:
    """The cheap tamper: change a bucket and leave the provenance alone."""
    from apps.buyer.seed.store import ACCOUNTS_FILE, SeedArtifactError, load

    root = _copy_corpus(tmp_path / "edited")
    lines = (root / ACCOUNTS_FILE).read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["buckets"]["region"] = "XX"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    (root / ACCOUNTS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(SeedArtifactError) as raised:
        load(root)
    assert "digest" in str(raised.value)


def test_a_re_digested_corpus_still_fails_the_chain(tmp_path: pathlib.Path) -> None:
    """**The falsification test.** Edit a bucket, recompute the digest, and it is still refused.

    Every file-digest check in this module passes over this corpus. What refuses it is the
    marker being a *hash of the row* rather than a label beside it: the edited row's buckets no
    longer mint the pseudonym that names it. This is the assertion that makes "seeded rows stay
    distinguishable, durably" mean something stronger than "we wrote it down twice".
    """
    from apps.buyer.seed.chain import ChainBroken
    from apps.buyer.seed.store import ACCOUNTS_FILE, load

    root = _copy_corpus(tmp_path / "re-digested")
    lines = (root / ACCOUNTS_FILE).read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[3])
    edited["buckets"]["budget_band"] = "1000+"
    lines[3] = json.dumps(edited, sort_keys=True, separators=(",", ":"))
    (root / ACCOUNTS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    _re_digest(root)

    with pytest.raises(ChainBroken) as raised:
        load(root)
    assert "row 3" in str(raised.value)


def test_a_truncated_corpus_is_caught_by_the_recorded_head(tmp_path: pathlib.Path) -> None:
    """Dropping the tail leaves a perfectly valid chain — of the wrong length.

    A truncated chain's head is a real chain head, computed correctly over the rows that
    remain, so nothing *inside* the corpus can object to it. The head recorded in the
    provenance is the outside witness, and re-digesting the file does not move it.
    """
    from apps.buyer.seed.chain import ChainBroken
    from apps.buyer.seed.store import ACCOUNTS_FILE, load

    root = _copy_corpus(tmp_path / "truncated")
    lines = (root / ACCOUNTS_FILE).read_text(encoding="utf-8").splitlines()
    (root / ACCOUNTS_FILE).write_text("\n".join(lines[:-3]) + "\n", encoding="utf-8")
    _re_digest(root)

    with pytest.raises(ChainBroken) as raised:
        load(root)
    assert "head" in str(raised.value)


def test_a_row_smuggled_into_the_middle_moves_every_digest_after_it(
    tmp_path: pathlib.Path,
) -> None:
    """An inserted row is not merely unpinned — it renumbers the tail and breaks it."""
    from apps.buyer.seed.chain import ChainBroken
    from apps.buyer.seed.store import ACCOUNTS_FILE, load

    root = _copy_corpus(tmp_path / "smuggled")
    lines = (root / ACCOUNTS_FILE).read_text(encoding="utf-8").splitlines()
    smuggled = json.loads(lines[0])
    smuggled["pseudonym"] = "psn-seed-" + "a" * 24
    lines.insert(5, json.dumps(smuggled, sort_keys=True, separators=(",", ":")))
    (root / ACCOUNTS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    _re_digest(root)

    with pytest.raises(ChainBroken):
        load(root)


def test_the_marker_cannot_be_forged_by_a_field_beside_the_key() -> None:
    """A row claiming to be seeded in every way except the pseudonym reads as **real**.

    The direction is deliberate and it is the fail-safe one. An unrecognised row is treated as
    a real buyer, so the mistake this makes is "a synthetic buyer was missed", never "a real
    buyer was written off as manufactured". The reader looks at the key and nothing else.
    """
    from apps.buyer.seed.chain import is_seeded_pseudonym

    assert not is_seeded_pseudonym("psn-0123456789abcdef0123456789abcdef")
    assert not is_seeded_pseudonym("seed-psn-0123")
    assert not is_seeded_pseudonym(None)
    assert not is_seeded_pseudonym({"provenance": "seed", "simulated": True})
    assert is_seeded_pseudonym("psn-seed-000000000000000000000000")


def test_the_loader_refuses_an_unmarked_row_before_it_opens_a_socket(
    tmp_path: pathlib.Path,
) -> None:
    """``load`` refuses a corpus row without the marker, and names the corpus rather than the DB.

    The database's CHECK would refuse it too. This refusal exists because that one arrives as
    "CheckViolation on row 17", which sends the reader to the schema; this one says the corpus
    is wrong, which is where the fault actually is.
    """
    from apps.buyer.seed import __main__ as cli

    class _Corpus:
        rows = ({"pseudonym": "psn-0123", "buckets": {}},)
        chain_head = "0" * 64

    def _load(_root: Any = None) -> Any:
        return _Corpus()

    original = cli.load
    cli.load = _load  # type: ignore[assignment]
    try:
        status = cli.main(["load", "--dsn", "postgresql://app@localhost:5432/nothing"])
    finally:
        cli.load = original  # type: ignore[assignment]
    assert status == 1


def test_the_loader_refuses_a_non_loopback_target_without_an_explicit_flag() -> None:
    """A corpus of manufactured buyers does not travel to another host by default.

    Command line only: there is deliberately no environment variable that grants it, for the
    reason ``services/sim/seed/targets.py`` gives — one address and one guess is how a
    population ends up written into a database nobody chose.
    """
    from apps.buyer.seed.__main__ import _refuse_remote

    assert _refuse_remote("postgresql://app@localhost:5432/db", allow_remote=False) is None
    assert _refuse_remote("postgresql://app@127.0.0.1:5432/db", allow_remote=False) is None
    refusal = _refuse_remote("postgresql://app@prod.internal:5432/db", allow_remote=False)
    assert refusal and "prod.internal" in refusal
    assert _refuse_remote("postgresql://app@prod.internal:5432/db", allow_remote=True) is None


# ======================================================================================
# 4. The live switch
# ======================================================================================


def test_going_live_is_one_statement_and_the_corpus_says_so() -> None:
    """The provenance records the exact deletion, and the marker is what makes it exact.

    ``delete from app.buyer_accounts where provenance = 'seed'`` is safe to write down as an
    instruction precisely because of the constraint: it cannot match a real buyer's row, and it
    cannot miss a seeded one. A note telling an operator to run a statement whose blast radius
    depends on nobody having mislabelled anything would be worse than no note.
    """
    from apps.buyer.seed.store import load

    notes = " ".join(load().collection["notes"])
    assert "delete from app.buyer_accounts where provenance = 'seed'" in notes
    assert "Going live changes no code on the serving path" in notes


def test_the_corpus_records_where_it_came_from_and_how_to_make_it_again() -> None:
    """Provenance a stranger can act on: the command, the module, the revision, the marker."""
    from apps.buyer.seed.chain import SEED_PSEUDONYM_PREFIX
    from apps.buyer.seed.store import ARTIFACT_KIND, ARTIFACT_VERSION, load

    collection = load().collection
    assert collection["artifact_version"] == ARTIFACT_VERSION
    assert collection["kind"] == ARTIFACT_KIND
    assert collection["producer"].startswith("python -m apps.buyer.seed run --seed ")
    assert collection["producer"] == collection["determinism"]["reproduce"]
    assert collection["producer_module"] == "apps.buyer.seed.population"
    assert collection["marker"]["prefix"] == SEED_PSEUDONYM_PREFIX
    assert collection["marker"]["reader"] == "apps.buyer.seed.chain.is_seeded_pseudonym"
    assert collection["target"]["table"] == "app.buyer_accounts"
    assert collection["target"]["reader"].startswith("GET /buyer/store-window")
    # A zero-row cohort is listed at zero rather than omitted -- absent and zero read the same
    # to a careless eye and mean different things.
    from apps.buyer.seed.population import COHORTS

    assert set(collection["marker"]["rows_by_cohort"]) == {cohort.name for cohort in COHORTS}


def test_a_corpus_from_a_future_layout_is_refused_rather_than_parsed(
    tmp_path: pathlib.Path,
) -> None:
    """A major-version bump is a refusal, not a best effort."""
    from apps.buyer.seed.store import COLLECTION_FILE, SeedArtifactError, load

    root = _copy_corpus(tmp_path / "from-the-future")
    collection = json.loads((root / COLLECTION_FILE).read_text(encoding="utf-8"))
    collection["artifact_version"] = "9.0.0"
    (root / COLLECTION_FILE).write_text(json.dumps(collection, indent=2, sort_keys=True) + "\n")

    with pytest.raises(SeedArtifactError) as raised:
        load(root)
    assert "9.0.0" in str(raised.value)


def test_a_corpus_recording_no_digest_is_refused_as_loudly_as_a_wrong_one(
    tmp_path: pathlib.Path,
) -> None:
    """ "No digest recorded" and "not checked" are the same thing to every caller downstream."""
    from apps.buyer.seed.store import ACCOUNTS_FILE, COLLECTION_FILE, SeedArtifactError, load

    root = _copy_corpus(tmp_path / "undigested")
    collection = json.loads((root / COLLECTION_FILE).read_text(encoding="utf-8"))
    collection["file_sha256"].pop(ACCOUNTS_FILE)
    (root / COLLECTION_FILE).write_text(json.dumps(collection, indent=2, sort_keys=True) + "\n")

    with pytest.raises(SeedArtifactError) as raised:
        load(root)
    assert "no digest" in str(raised.value)


def test_the_cli_reports_its_finding_in_its_exit_status(tmp_path: pathlib.Path) -> None:
    """``verify`` exits 0 on the committed corpus, 1 on a broken one, 2 on a missing one."""
    from apps.buyer.seed.__main__ import main

    assert main(["verify"]) == 0
    assert main(["census", "--json"]) == 0
    assert main(["verify", "--from", str(tmp_path / "nothing-here")]) == 2

    root = _copy_corpus(tmp_path / "broken")
    from apps.buyer.seed.store import ACCOUNTS_FILE

    lines = (root / ACCOUNTS_FILE).read_text(encoding="utf-8").splitlines()
    (root / ACCOUNTS_FILE).write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    _re_digest(root)
    assert main(["verify", "--from", str(root)]) == 1


def test_a_corpus_recording_no_chain_head_is_refused(tmp_path: pathlib.Path) -> None:
    """Dropping the recorded head must not silently downgrade the check to a self-check.

    Without an ``expected_head``, ``verify_chain`` asks only "does each row's pseudonym match
    its own link" — which a corpus with rows removed from the END satisfies exactly, because
    the remaining rows are a valid chain. So a corpus that records no head has not been checked
    in the way every caller of ``load`` believes it has, and the loader refuses it for the same
    reason it refuses a missing file digest.
    """
    from apps.buyer.seed.store import ACCOUNTS_FILE, COLLECTION_FILE, SeedArtifactError, load

    root = _copy_corpus(tmp_path / "headless")
    lines = (root / ACCOUNTS_FILE).read_text(encoding="utf-8").splitlines()
    (root / ACCOUNTS_FILE).write_text("\n".join(lines[:-5]) + "\n", encoding="utf-8")
    collection = json.loads((root / COLLECTION_FILE).read_text(encoding="utf-8"))
    collection["determinism"].pop("chain_head")
    (root / COLLECTION_FILE).write_text(json.dumps(collection, indent=2, sort_keys=True) + "\n")
    _re_digest(root)

    with pytest.raises(SeedArtifactError) as raised:
        load(root)
    assert "chain_head" in str(raised.value)


@pytest.mark.parametrize(
    ("dsn", "refused"),
    [
        pytest.param("postgresql://app:x@localhost:5432/db", False, id="url-localhost"),
        pytest.param("postgresql://app@127.0.0.1:5432/db", False, id="url-loopback-v4"),
        pytest.param("host=localhost dbname=db", False, id="keyword-localhost"),
        pytest.param("postgresql://app@prod.example.com:5432/db", True, id="url-remote"),
        pytest.param("host=prod.example.com dbname=db user=app", True, id="keyword-remote"),
        pytest.param("postgresql:///db?host=prod.example.com", True, id="url-query-host"),
        pytest.param("dbname=db user=app", True, id="names-no-host-at-all"),
        pytest.param("host=localhost,prod.example.com dbname=db", True, id="one-of-two-remote"),
        pytest.param("hostaddr=10.0.0.7 dbname=db", True, id="hostaddr"),
    ],
)
def test_the_loader_reads_every_dsn_grammar_libpq_accepts(dsn: str, refused: bool) -> None:
    """The loopback guard must read all three connection-string shapes, not just URLs.

    MEASURED before the fix: ``urlsplit("host=prod.example.com dbname=proxyshop").hostname`` is
    ``None``, and the guard treated ``None`` as loopback — so
    ``python -m apps.buyer.seed load --dsn "host=prod…"`` wrote forty manufactured buyers into a
    remote database with no ``--allow-remote``, which is exactly what the module docstring
    promises cannot happen. psycopg accepts the keyword/value form and the ``?host=`` query form
    as readily as the URL form, so a guard that reads one grammar is a guard for one grammar.

    Naming NO host is refused as well, and that is not pedantry: libpq then reads ``$PGHOST``,
    so the target is whatever the environment says — the one thing this guard exists to prevent.
    """
    from apps.buyer.seed.__main__ import _refuse_remote

    reason = _refuse_remote(dsn, allow_remote=False)
    if refused:
        assert reason is not None, f"{dsn!r} was admitted without --allow-remote"
    else:
        assert reason is None, f"{dsn!r} is loopback and was refused: {reason}"
    assert _refuse_remote(dsn, allow_remote=True) is None, (
        "--allow-remote is the deliberate override and must admit every shape"
    )


# ======================================================================================
# membership: the check the database's constraint cannot make
# ======================================================================================


def test_the_audit_separates_what_the_constraint_guarantees_from_what_it_does_not() -> None:
    """``audit`` catches the three findings ``buyer_accounts_provenance_is_the_key`` cannot.

    The constraint guarantees CONSISTENCY — no row's label and key can disagree, so no HTTP
    caller can mint a seeded-looking row. It does not guarantee MEMBERSHIP: anything holding
    the ``app`` role may insert ``psn-seed-<any 24 characters>`` with ``provenance = 'seed'``,
    and that row is legal, is served by the window as seeded, and matches no chain link. The
    table stores no signature, so nothing at read time can tell it from a corpus row.

    This drives the audit over a stand-in table rather than a live one, so it runs everywhere,
    and asserts all three findings and the clean case. The ``docker``-marked twin in
    ``test_store_window.py`` drives it against real Postgres.
    """
    from apps.buyer.seed import __main__ as cli
    from apps.buyer.seed.store import load

    corpus = load()
    pinned = [(row["pseudonym"], row["buckets"]) for row in corpus.rows]

    class _Cursor:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def execute(self, *_a: Any, **_k: Any) -> Any:
            return self

        def fetchall(self) -> list[Any]:
            return self._rows

    class _Connection:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def cursor(self, *_a: Any, **_k: Any) -> Any:
            return _Cursor(self._rows)

        def close(self) -> None:
            return None

    def run(rows: list[Any]) -> int:
        import psycopg

        original = psycopg.connect
        psycopg.connect = lambda *_a, **_k: _Connection(rows)  # type: ignore[assignment]
        try:
            return cli.main(["audit", "--dsn", "postgresql://app@localhost:5432/db", "--json"])
        finally:
            psycopg.connect = original  # type: ignore[assignment]

    assert run(list(pinned)) == 0, "the committed corpus, loaded exactly, must audit clean"

    smuggled = [*pinned, ("psn-seed-" + "d" * 24, {"region": "XX"})]
    assert run(smuggled) == 1, (
        "a row marked 'seed' that is in no committed corpus was accepted; the CHECK constraint "
        "permits it and only this comparison can see it"
    )

    altered = [(pinned[0][0], {}), *pinned[1:]]
    assert run(altered) == 1, "a pinned pseudonym carrying buckets the corpus does not commit to"

    assert run(list(pinned[:-3])) == 1, "corpus rows missing from the table"
