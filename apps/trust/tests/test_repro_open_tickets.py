"""Reproduction gates for the open apps/trust tickets.

Every test here asserts the behaviour that SHOULD hold and is marked
``@pytest.mark.xfail(strict=True)`` while the defect is live. A normal run reports
``xfailed`` and stays green; the ticket's gate runs
``pytest apps/trust/tests/test_repro_open_tickets.py -q --runxfail -k <name>`` and gets a
real red. ``strict=True`` means the marker cannot outlive the fix: once the defect is gone
the test XPASSes, which fails the normal run and forces whoever fixed it to delete the
marker.

Nothing here is allowed to skip. A skip is not a gate, and the compose datastore stack is
routinely down in this repo, so every assertion below is made against source, against pure
functions, or against a subprocess — never against a live datastore.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TRUST_SRC = REPO_ROOT / "apps" / "trust" / "src"
TRUST_TESTS = REPO_ROOT / "apps" / "trust" / "tests"

#: The instant every score below is decayed against. Explicit, never a clock.
AS_OF = "2026-01-01T00:00:00Z"


def _product_python_files() -> list[pathlib.Path]:
    """Every shipped ``.py`` file — apps, packages, services, e2e — excluding tests.

    Excludes ``**/tests/**``, ``conftest.py`` and ``.venv``/``node_modules``. This is the
    "product code" set the tickets' own ``git grep`` reproductions are written against.
    """
    roots = ("apps", "packages", "services", "e2e", "proxyshop_support")
    found: list[pathlib.Path] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            parts = set(path.parts)
            if "tests" in parts or ".venv" in parts or "node_modules" in parts:
                continue
            if path.name == "conftest.py":
                continue
            found.append(path)
    return sorted(found)


# ======================================================================================
# T-154 — the DB-backed trust suite connects as a principal the deployment does not use
# ======================================================================================
def test_the_events_fixture_connects_as_the_role_the_ledger_writer_actually_ships_as() -> None:
    """The DB-backed trust fixtures must use the deployment's own principal.

    ``EVENTS_ROLE`` is what ``_fixtures_events.events_dsn`` hands every Postgres-backed
    test in this package. Its comment says ``app`` is "the role the writer connects as ...
    deliberately not ``trust_rw``". That is no longer true: ``DEFAULT_DSN_ENV`` names
    ``PROXYSHOP_PG_DSN_TRUST_RW`` — ``trust_rw``'s own variable — ahead of the generic app
    DSN, ``.env.example`` and ``compose.yaml`` set it, and ``0004`` grants the two roles
    genuinely different (not nested) privileges: ``app`` gets SELECT+INSERT on ``ledger``
    plus full DML on ``app`` and ``sealed``; ``trust_rw`` gets full DML on ``ledger``,
    read-only on ``app`` and nothing in ``sealed``.

    The expected role is DERIVED from the writer's own resolution order rather than typed
    as a literal, so this grades "the fixtures use the shipped principal", not "the
    fixtures use a string spelled this way today".
    """
    from apps.trust.src.events.pg import DEFAULT_DSN_ENV
    from apps.trust.tests import _fixtures_events
    from proxyshop_support.postgres import ROLES

    env_to_role = {spec[0]: role for role, spec in ROLES.items()}
    shipped_role = next(
        (env_to_role[name] for name in DEFAULT_DSN_ENV if name in env_to_role), None
    )
    assert shipped_role is not None, (
        "no entry of DEFAULT_DSN_ENV names a per-role DSN variable, so this test cannot "
        "derive the shipped principal — the resolution order changed shape"
    )

    assert _fixtures_events.EVENTS_ROLE == shipped_role, (
        f"the DB-backed trust fixtures connect as {_fixtures_events.EVENTS_ROLE!r}, but the "
        f"ledger writer ships as {shipped_role!r} (first per-role variable in "
        f"{list(DEFAULT_DSN_ENV)}). The two grant sets are not nested, so every DB-backed "
        f"test in apps/trust is evidence about a principal the deployment never uses."
    )


# ======================================================================================
# T-166 — a conditional test that silently stopped covering the case it was written for
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-166: test_replay_with_snapshots_names_the_missing_scorer_rather_than_returning_"
        "nothing still branches on `if response.status_code == 503`, a branch the landed "
        "T-062 scorer makes unreachable, so half the test is dead; remove this marker with "
        "the fix"
    ),
)
def test_the_replay_snapshot_test_does_not_branch_on_a_status_it_can_never_reach() -> None:
    """A test that branches on its own subject's status covers whichever branch it took.

    ``test_replay_with_snapshots_names_the_missing_scorer_rather_than_returning_nothing``
    was written while the scorer did not exist and forgives a 503. T-062 landed a real
    scorer, so the 503 arm can no longer execute and the assertions inside it — that the
    body names ``scorer_unavailable`` and mentions T-062 — grade nothing at all. The test
    must now assert the 200 outcome unconditionally.

    Parsed with ``ast`` rather than matched as text, so reformatting the source cannot make
    the branch invisible to this gate.
    """
    from apps.trust.tests import test_events

    subject = test_events.test_replay_with_snapshots_names_the_missing_scorer_rather_than_returning_nothing
    tree = ast.parse(textwrap.dedent(inspect.getsource(subject)))

    branches = [
        ast.unparse(node.test)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "status_code" in ast.unparse(node.test)
    ]
    assert branches == [], (
        f"{subject.__name__} still branches on the status code it is testing "
        f"({branches}). With T-062 landed the 503 arm is unreachable, so its assertions "
        f"about `scorer_unavailable` never run — the test passes by taking the other arm. "
        f"Assert the 200 outcome unconditionally instead."
    )


# ======================================================================================
# T-167 — four byte-identical copies of the module-binding shim
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-167: apps/trust/src/{scoring,reconcile,feedback,snapshot}/_binding.py are four "
        "byte-identical copies of the elected-primary shim with nothing enforcing that they "
        "stay identical; remove this marker with the fix"
    ),
)
def test_the_elected_primary_binding_shim_has_exactly_one_home() -> None:
    """The dual-spelling module-identity fix must exist once, not four times.

    The shim has to run before the first relative import in each feature package, so it was
    copied into each one. Nothing enforces that the copies agree, and a fix applied to one
    and not the others silently reintroduces the module-identity bug the shim exists to
    prevent — a bug this repo already paid for once.

    Symlinks are resolved before counting, so hoisting the shim to a shared module OR
    pointing the four paths at one real file both satisfy this; four independent regular
    files do not.
    """
    copies = sorted(TRUST_SRC.glob("*/_binding.py"))
    assert copies, "no _binding.py shim found at all — this gate is pointed at the wrong path"

    bodies = {path.read_bytes() for path in copies}
    real_files = {path.resolve() for path in copies}

    assert len(real_files) <= 1, (
        f"the elected-primary binding shim exists as {len(real_files)} independent files "
        f"({[str(p.relative_to(REPO_ROOT)) for p in copies]}), holding "
        f"{len(bodies)} distinct body/bodies. Nothing asserts they agree, so a fix applied "
        f"to one copy and not the others reintroduces the dual-spelling module-identity bug "
        f"the shim exists to prevent. Hoist it to a single module."
    )


# ======================================================================================
# T-181 — the DSN precedence order, defended at more than one point (NOT an xfail)
# ======================================================================================
def test_every_pair_of_ledger_dsn_variables_resolves_to_the_earlier_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-181's missing second precedence assertion. Green today, deliberately.

    T-181 records that the ORDER of ``DEFAULT_DSN_ENV`` rests on a single test: reordering
    the tuple, and walking a correct tuple in the wrong order, each produce exactly one red.
    The live connected-principal test cannot see order at all — it clears every DSN variable
    and sets one.

    This is the second assertion, and it is derived rather than enumerated: for EVERY pair
    ``(earlier, later)`` in the resolution order, setting both must resolve to ``earlier``.
    That covers the pairs the existing test does not, and it grades behaviour rather than
    the tuple's source text, so it survives a reordering only if the resolution really does
    walk the tuple in order.

    Not marked xfail: the behaviour it asserts is already correct, and T-181 is a coverage
    gap rather than a live defect. It cannot have a red gate for that reason.
    """
    from apps.trust.src.events.pg import DEFAULT_DSN_ENV, PostgresEventStore

    for index, earlier in enumerate(DEFAULT_DSN_ENV):
        for later in DEFAULT_DSN_ENV[index + 1 :]:
            for name in DEFAULT_DSN_ENV:
                monkeypatch.delenv(name, raising=False)
            monkeypatch.setenv(earlier, f"postgresql://{earlier.lower()}@db.example:5432/w0")
            monkeypatch.setenv(later, f"postgresql://{later.lower()}@db.example:5432/w0")

            resolved = PostgresEventStore()._resolve_dsn()
            assert resolved == f"postgresql://{earlier.lower()}@db.example:5432/w0", (
                f"with {earlier} and {later} both set the writer resolved {resolved!r}; "
                f"{earlier} is listed first in DEFAULT_DSN_ENV and must win"
            )


# ======================================================================================
# T-193 — the deployed trust image cannot verify a claim
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-193: apps/trust/Dockerfile does not COPY packages/verification, so "
        "trust.verification.verify raises ModuleNotFoundError inside the shipped image; "
        "remove this marker with the fix"
    ),
)
def test_the_trust_image_copy_set_can_resolve_the_claim_verifier(tmp_path: Any) -> None:
    """T-065's verifier must be present in the artifact that ships.

    Built against a container-shaped tree rather than against a running daemon: the
    Dockerfile's own ``COPY`` lines are parsed, those paths are materialised into a temp
    tree, the ``.pkgroot`` symlinks the Dockerfile's ``RUN`` creates are recreated *from
    that RUN's own* ``ln -s`` *pairs*, and a subprocess with the image's ``PYTHONPATH``
    (and nothing else) asks ``trust.verification`` for ``verify``.

    ``import trust.verification`` alone is NOT the reproduction any more — the module went
    lazy (PEP 562) precisely so the absent package would not kill the service at import
    time. The failure moved to the first call, which is worse to find and no less fatal:
    a trust service that cannot verify a claim treats every product-fact claim as
    unverifiable, silently.

    Two things make this an honest measurement of the temp tree rather than of the repo,
    and both were established by measurement after this gate was found XPASSing while the
    defect it describes was still live:

    ``-S`` **on the probe.** ``.venv/lib/python3.12/site-packages/_proxyshop.pth`` lists the
    checkout root and its ``.pkgroot``. ``site`` processes ``.pth`` files at interpreter
    startup, so those two directories land on ``sys.path`` *regardless of* ``PYTHONPATH``
    and regardless of ``cwd`` — the probe resolved ``claim_verification`` out of the LIVE
    CHECKOUT and printed ``RESOLVED`` while the image would have raised. ``-S`` disables
    ``site`` and therefore ``.pth`` handling; the verifier is stdlib-only (``hashlib``,
    ``json``, ``math``, ``re``, ``datetime``, ``types``, ``typing``, ``collections``), so
    losing ``site-packages`` costs the probe nothing it needs. ``-E`` is NOT usable here —
    it discards ``PYTHONPATH`` too, and the probe then fails on ``No module named 'trust'``,
    which is the wrong red. ``-I`` is NOT usable either: it implies ``-E -s``, and ``-s``
    suppresses only the *user* site directory, leaving the venv's ``.pth`` to fire — measured
    RESOLVING against the unfixed Dockerfile, i.e. maximally wrong.

    **Symlinks parsed, not hardcoded.** The two ``.pkgroot`` links used to be a literal
    tuple of ``contracts`` and ``trust``. ``packages/verification/__init__.py`` forwards to
    ``claim_verification``, which the repo provides as ``.pkgroot/claim_verification ->
    ../packages/verification/src``, so closing T-193 takes a ``COPY`` *and* a matching
    ``ln -s``. Against a hardcoded pair the fixed Dockerfile still measured FAILS — the gate
    could not see its own fix. Reading the pairs off the ``RUN`` makes the witness fire.
    """
    dockerfile = (REPO_ROOT / "apps" / "trust" / "Dockerfile").read_text(encoding="utf-8")
    copies = re.findall(r"^COPY\s+(\S+)\s+(\S+)\s*$", dockerfile, flags=re.MULTILINE)
    assert copies, "no COPY instructions parsed out of apps/trust/Dockerfile"

    app = tmp_path / "app"
    app.mkdir()
    for source, destination in copies:
        src = REPO_ROOT / source
        dst = app / destination.removeprefix("/app/").rstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(
                src, dst, dirs_exist_ok=True, symlinks=False, ignore_dangling_symlinks=True
            )
        elif src.is_file():
            shutil.copy2(src, dst)

    # Every `ln -s <target> <link>` the image's RUN layer creates under /app, read off the
    # Dockerfile rather than transcribed, so a link ADDED by a fix is reproduced here too.
    links = [
        (target, link)
        for target, link in re.findall(r"\bln\s+-s\s+(\S+)\s+(\S+)", dockerfile)
        if link.startswith("/app/")
    ]
    assert links, (
        "no `ln -s ... /app/...` pairs parsed out of apps/trust/Dockerfile, so this gate "
        "would build a tree with none of the .pkgroot symlinks that make the flat layout "
        "importable (R1b/D42) and would go red whatever the COPY set contains"
    )
    for target, link in links:
        spot = app / link.removeprefix("/app/")
        spot.parent.mkdir(parents=True, exist_ok=True)
        if not spot.is_symlink() and not spot.exists():
            spot.symlink_to(target)

    probe = textwrap.dedent(
        """
        import json, sys
        print("SYSPATH " + json.dumps(sys.path))
        import trust.verification as shim
        print("SHIM " + shim.__file__)
        shim.verify
        print("RESOLVED")
        """
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": f"{app}:{app / '.pkgroot'}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        # -S: no `site`, therefore no `.pth`, therefore no live checkout on sys.path.
        [sys.executable, "-S", "-c", probe],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    def _tagged(tag: str) -> str | None:
        prefix = f"{tag} "
        return next(
            (line[len(prefix) :] for line in result.stdout.splitlines() if line.startswith(prefix)),
            None,
        )

    def _inside_checkout(entry: str) -> bool:
        if not entry:
            return False
        resolved = pathlib.Path(entry).resolve()
        return resolved == REPO_ROOT or REPO_ROOT in resolved.parents

    reported_path = _tagged("SYSPATH")
    assert reported_path is not None, (
        "the container-shaped probe never reached its first statement, so this gate graded "
        f"nothing at all:\n{result.stderr.strip()[-1200:]}"
    )

    leaked = [entry for entry in json.loads(reported_path) if _inside_checkout(entry)]
    assert leaked == [], (
        f"the probe subprocess can see the live checkout on sys.path ({leaked}), so it can "
        f"import the claim verifier from the repo instead of from the container-shaped tree "
        f"and this gate proves nothing about the image. .venv's site-packages/_proxyshop.pth "
        f"puts {REPO_ROOT} and {REPO_ROOT / '.pkgroot'} on sys.path at interpreter startup "
        f"regardless of PYTHONPATH; `-S` is what keeps them off."
    )

    shim_file = _tagged("SHIM")
    assert shim_file is not None, (
        "the container-shaped tree cannot import `trust.verification` at all, so this gate "
        "is going red BEFORE the verifier lookup it exists to grade — fix the tree, not the "
        f"COPY set:\n{result.stderr.strip()[-1200:]}"
    )
    assert app.resolve() in pathlib.Path(shim_file).resolve().parents, (
        f"the probe imported trust.verification from {shim_file}, which is outside the "
        f"container-shaped tree at {app.resolve()} — it is grading the live checkout"
    )

    assert result.returncode == 0 and "RESOLVED" in result.stdout, (
        "the trust image's COPY set cannot resolve `trust.verification.verify`:\n"
        f"{result.stderr.strip()[-1200:]}\n"
        "apps/trust/Dockerfile copies packages/contracts, proxyshop_support and "
        "apps/trust/src, and NOT packages/verification — so T-065's engine, whose golden "
        "set passes 36/36 in the repo, is absent from the artifact that deploys."
    )


# ======================================================================================
# T-207 — the weight table's gloss contradicts the approved manifest
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-207: scoring/engine.py glosses mismatch_return as 'the buyer said it matched and "
        "then returned it', while the approved manifest defines it as the buyer reporting a "
        "MISMATCH; remove this marker with the fix"
    ),
)
def test_the_mismatch_return_gloss_agrees_with_the_approved_manifest() -> None:
    """Load-bearing documentation next to a number that decides trust must be true.

    ``fixtures/manifest.json`` is the human-approved ground truth (SPEC A3). Its
    ``dishonest_store`` script defines the behaviour that produces ``mismatch_return`` as
    ``pitch_delivery_mismatch``: "The buyer reports that what arrived does not match what
    was pitched, and returns it." The engine's weight table says the opposite — that the
    buyer *said it matched* — which inverts what the 1.5 weight is being applied to.
    """
    from apps.trust.src.scoring import engine

    manifest = json.loads((REPO_ROOT / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    behaviours = (
        (manifest.get("dishonest_store") or {}).get("behaviours")
        or (manifest.get("dishonest_store") or {}).get("behaviors")
        or []
    )
    approved = [
        row for row in behaviours if isinstance(row, dict) and row.get("type") == "mismatch_return"
    ]
    assert approved, (
        "the approved manifest no longer scripts a mismatch_return behaviour, so this gate "
        "cannot read the authority it compares against"
    )
    description = str(approved[0].get("description", ""))
    assert "does not match" in description, (
        f"the manifest's mismatch_return description changed shape: {description!r}"
    )

    gloss = "\n".join(
        line for line in (engine.__doc__ or "").splitlines() if "mismatch_return" in line
    )
    assert gloss, "scoring/engine.py no longer glosses mismatch_return at all"

    assert "said it matched" not in gloss, (
        f"scoring/engine.py glosses mismatch_return as {gloss.strip()!r}, i.e. the buyer "
        f"said the delivery MATCHED. The approved manifest says the opposite: "
        f"{description!r}. One of the two decides a 1.5-weight negative observation, and "
        f"the manifest is the authority (SPEC A3)."
    )


# ======================================================================================
# T-212 — a database-backed test with no docker marker
# ======================================================================================
#: Fixtures that open, migrate or clean a real Postgres database. A test requesting any of
#: them touches the datastore and must therefore carry ``@pytest.mark.docker``, which is
#: what T-109's per-service reachability inference keys on.
_DATASTORE_FIXTURES = frozenset(
    {
        "ledger_migrated",
        "ledger_clean",
        "worker_database",
        "pg_admin",
        "pg_role",
        "events_store",
        "events_client",
        "redis_client",
        "neo4j_driver",
    }
)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-212: test_events_hardening.py's two live-database tests request datastore "
        "fixtures but carry no @pytest.mark.docker, so T-109's per-service reachability "
        "inference cannot route them and they skip through the fixture nag path instead; "
        "remove this marker with the fix"
    ),
)
def test_every_database_backed_hardening_test_carries_the_docker_marker() -> None:
    """A test that needs a datastore must say so with the marker, not with a fixture nag.

    ``conftest._require`` skips an unmarked datastore test with the message "mark this test
    @pytest.mark.docker so it skips cleanly" — the repo's own machinery reporting the
    defect. Marked tests are classified per service at collection time (T-109) and skip with
    a reason naming the endpoint; unmarked ones run under ``verify.sh check``, get as far as
    fixture setup, and skip only because a fixture happened to notice.

    Read off the module's own function objects: ``pytest.mark.docker`` applied as a
    decorator lands in ``__wrapped__``-free ``pytestmark``, so this sees exactly what pytest
    sees.
    """
    from apps.trust.tests import test_events_hardening as module

    unmarked: list[str] = []
    for name, function in vars(module).items():
        if not name.startswith("test_") or not callable(function):
            continue
        requested = set(inspect.signature(function).parameters)
        if not (requested & _DATASTORE_FIXTURES):
            continue
        marks = {mark.name for mark in getattr(function, "pytestmark", ())}
        if "docker" not in marks:
            unmarked.append(f"{name}({sorted(requested & _DATASTORE_FIXTURES)})")

    assert unmarked == [], (
        "these tests in apps/trust/tests/test_events_hardening.py open a real database but "
        f"carry no @pytest.mark.docker: {unmarked}. Without the marker T-109's per-service "
        "inference cannot route them to postgres, so they cannot skip cleanly when the "
        "stack is down and they run inside `verify.sh check` unprotected."
    )


# ======================================================================================
# T-237 — the S2 chain has no product code closing it
# ======================================================================================
#: The two frozen ledger kinds that record a delisting decision. Reserved in
#: ``db/migrations/0002``, in the protocol schema and in ``trust.events.LEDGER_EVENT_KINDS``.
_BLACKLIST_EVENT_KINDS = ("blacklisted", "blacklist_expired")

#: A string literal used as an event KIND, in any of the spellings this repo emits events
#: with: a ``"kind": "blacklisted"`` dict entry, a ``kind="blacklisted"`` keyword, or a
#: module constant named ``*_KIND``.
_KIND_EMISSION = re.compile(
    r"""(["']kind["']\s*:\s*["'](?:blacklisted|blacklist_expired)["'])"""
    r"""|(\bkind\s*=\s*["'](?:blacklisted|blacklist_expired)["'])"""
    r"""|(_KIND\s*(?::[^=\n]+)?=\s*["'](?:blacklisted|blacklist_expired)["'])"""
)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-237: nothing in product code turns a sub-threshold trust score into a blacklisted "
        "ledger event — the two frozen kinds are reserved everywhere and emitted nowhere, so "
        "'the exchange stops asking the dishonest store' is a seam nobody implemented; "
        "remove this marker with the fix"
    ),
)
def test_a_sub_threshold_trust_score_produces_a_blacklisted_ledger_event() -> None:
    """S2's second half: the engine catches the store, and then something acts on it.

    The first half is real and measured here: a store with contradicted observations across
    the five reachable dimensions scores far below ``BLACKLIST_THRESHOLD``. The second half
    is missing. ``blacklisted`` and ``blacklist_expired`` are two of the eighteen frozen
    ledger kinds — reserved in ``db/migrations/0002``, in ``protocol.schema.json`` and in
    ``trust.events.LEDGER_EVENT_KINDS`` — and no product module emits either. The
    consequence is that the delisting decision is never recorded anywhere the exchange, an
    auditor or an appeal can read it.

    The companion gaps, which the same fix has to close and which this gate does not
    separately assert: ``configure_auctions(app, eligibility=...)`` is only ever handed an
    empty ``StaticSellerEligibility`` (``exchange/auction/routes.py``), and the only code
    that turns a score into a status is ``services/sim/src/runner.py``, which had to compute
    it itself to demonstrate S2 end to end.
    """
    from trust.events import LEDGER_EVENT_KINDS
    from trust.scoring import BLACKLIST_THRESHOLD, Blacklist
    from trust.snapshot import build_snapshot

    dishonest = [
        {"store_id": "s-bad", "dim": dim, "type": "contradicted", "observed_at": AS_OF}
        for dim in (
            "price_honored",
            "discount_honored",
            "shipped_on_time",
            "not_returned",
            "catalog_claim_accuracy",
        )
        for _ in range(30)
    ]
    store = {"store_id": "s-bad", "business_identity": "bad-co", "observations": dishonest}
    snapshot = build_snapshot([store], blacklist=Blacklist(), as_of=AS_OF)
    entry = snapshot["stores"]["s-bad"]

    # Measured, and it holds: the engine catches the dishonest store.
    assert entry["score"] < BLACKLIST_THRESHOLD, (
        f"the dishonest store scored {entry['score']}, not below the published threshold "
        f"{BLACKLIST_THRESHOLD} — this gate's premise no longer holds"
    )
    assert set(_BLACKLIST_EVENT_KINDS) <= set(LEDGER_EVENT_KINDS)

    emitters = [
        str(path.relative_to(REPO_ROOT))
        for path in _product_python_files()
        if _KIND_EMISSION.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]

    assert emitters, (
        f"the store scores {entry['score']}, well below BLACKLIST_THRESHOLD "
        f"{BLACKLIST_THRESHOLD}, and its snapshot entry still reads "
        f"blacklisted={entry['blacklisted']}. No product module anywhere in apps/, "
        f"packages/, services/ or e2e/ emits a {list(_BLACKLIST_EVENT_KINDS)} ledger event, "
        f"so the delisting is never recorded and nothing downstream can read it. 'The trust "
        f"engine catches the dishonest store' is true; 'and therefore the exchange stops "
        f"asking it' is a seam nobody implemented (S2)."
    )
