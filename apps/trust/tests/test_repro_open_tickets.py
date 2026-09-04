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
#: The attribute an HTTP response carries its status on, in every client this repo uses.
_STATUS_ATTRIBUTE = "status_code"

#: How many preceding word tokens a negator may sit in and still govern a "match" claim.
_NEGATION_WINDOW = 4


def _reads_a_status_code(node: ast.AST, tainted: frozenset[str]) -> bool:
    """True when this expression reads a response status **however it is spelled**.

    Three spellings, because the substring check this replaced saw only the first:

    * the attribute itself — ``response.status_code``;
    * the attribute name as a string — ``getattr(response, "status_code")``;
    * a local that was ASSIGNED from one — ``code`` after ``code = response.status_code``.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == _STATUS_ATTRIBUTE:
            return True
        if isinstance(child, ast.Constant) and child.value == _STATUS_ATTRIBUTE:
            return True
        if isinstance(child, ast.Name) and child.id in tainted:
            return True
    return False


def _names_holding_a_status_code(tree: ast.AST) -> frozenset[str]:
    """Every local name that ends up holding a status code, to a fixed point.

    Iterated rather than single-pass so a chain — ``code = response.status_code`` then
    ``alias = code`` — is followed all the way, in either source order.
    """
    tainted: set[str] = set()
    while True:
        grew = False
        for node in ast.walk(tree):
            targets: list[ast.expr]
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
                targets, value = [node.target], node.value  # type: ignore[list-item]
            else:
                continue
            if value is None or not _reads_a_status_code(value, frozenset(tainted)):
                continue
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and name.id not in tainted:
                        tainted.add(name.id)
                        grew = True
        if not grew:
            return frozenset(tainted)


def _branches_on_a_status_code(tree: ast.AST) -> list[str]:
    """The source of every branch condition in ``tree`` that is decided by a status code.

    ``if``/``elif``, conditional expressions, ``while`` and ``match`` subjects all count: a
    dead arm is dead whichever of them guards it.
    """
    tainted = _names_holding_a_status_code(tree)
    conditions: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If | ast.IfExp | ast.While):
            conditions.append(node.test)
        elif isinstance(node, ast.Match):
            conditions.append(node.subject)
    return [ast.unparse(test) for test in conditions if _reads_a_status_code(test, tainted)]


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

    STRENGTHENED (T-288). The original walk below collected ``ast.If`` nodes whose test
    expression SOURCE contained the substring ``"status_code"``, which graded the spelling
    rather than the value. Measured evasion, which used to pass and is re-run against this
    gate in ``test_the_status_code_branch_gate_is_not_evadable_by_hoisting_the_attribute``::

        code = response.status_code      # the attribute leaves the `if`
        if code == 503:                  # ... and the dead branch survives untouched
            ...

    That yielded ``branches == []`` and went GREEN — and, under ``xfail(strict=True)``, an
    XPASS forces the marker's removal, so the escape hatch would have retired T-166 with the
    dead branch still there. The original assertion is UNCHANGED and still runs first; the
    second one below tracks the value through assignment to a fixed point, and also counts
    conditional expressions, ``while`` and ``match``, which the first never looked at.
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

    tracked = _branches_on_a_status_code(tree)
    assert tracked == [], (
        f"{subject.__name__} still branches on a value that came from the status code it is "
        f"testing ({tracked}). The condition need not MENTION `status_code` for the branch "
        f"to be dead — hoisting the attribute into a local moves the spelling, not the "
        f"unreachable arm. Assert the 200 outcome unconditionally instead."
    )


def test_the_status_code_branch_gate_is_not_evadable_by_hoisting_the_attribute() -> None:
    """The gate above, attacked. A tightened gate nobody attacked is not tightened.

    Each case below is a real evasion of the substring walk this gate used to be: the dead
    503 branch survives verbatim and only the SPELLING of its condition moves. Every one was
    measured GREEN against ``"status_code" in ast.unparse(node.test)`` and must now be red.
    The last case is the shape a genuine fix has, and must stay green — a gate that flags
    the fix is no better than one that misses the defect.
    """
    evasions = {
        "the defect as written today": "if response.status_code == 503:\n    pass\n",
        "hoisted into a local": "code = response.status_code\nif code == 503:\n    pass\n",
        "hoisted twice": (
            "code = response.status_code\nalias = code\nif alias == 503:\n    pass\n"
        ),
        "read through getattr": (
            'code = getattr(response, "status_code")\nif code == 503:\n    pass\n'
        ),
        "hoisted and moved into a conditional expression": (
            "code = response.status_code\nbody = a if code == 503 else b\n"
        ),
        "hoisted and moved into a match": (
            "code = response.status_code\nmatch code:\n    case 503:\n        pass\n"
        ),
        "tuple-unpacked": (
            "code, text = response.status_code, response.text\nif code == 503:\n    pass\n"
        ),
    }
    missed = [
        label
        for label, source in evasions.items()
        if not _branches_on_a_status_code(ast.parse(textwrap.dedent(source)))
    ]
    assert missed == [], (
        f"these rewrites keep the unreachable 503 branch and slip past the gate: {missed}. "
        f"The gate is grading how the condition is SPELLED again, which is the hole T-288 "
        f"measured."
    )

    fixed = "assert response.status_code == 200, response.text\n"
    assert _branches_on_a_status_code(ast.parse(fixed)) == [], (
        "the gate flags an unconditional assertion on the status code, which is exactly what "
        "T-166 asks the target test to become — it must not be red against its own fix"
    )


# ======================================================================================
# T-167 — four byte-identical copies of the module-binding shim
# ======================================================================================
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
        "T-065's engine, whose golden set passes 36/36 in the repo, is absent from the "
        "artifact that deploys. Closing this takes BOTH halves, because the flat layout "
        "needs both (R1b/D42): `COPY packages/verification/{__init__.py,src/}` AND "
        "`ln -s ../packages/verification/src /app/.pkgroot/claim_verification` in the RUN "
        "layer. A COPY without the link leaves the package unimportable, and the link "
        "without the COPY dangles."
    )


# ======================================================================================
# T-207 — the weight table's gloss contradicts the approved manifest
# ======================================================================================
#: A claim about whether a delivery matched its pitch. ``mismatch``/``mismatched`` carry the
#: negation in the word itself; everything else needs a negator in front of it. Word
#: boundaries keep IDENTIFIERS out: ``mismatch_return`` and ``feedback_match`` are names of
#: things, not sentences about them, and ``_`` is a word character so neither matches.
_MATCH_CLAIM = re.compile(r"\b(?P<denied>mis)?match(?:e[ds]|es|ing)?\b", re.IGNORECASE)

#: Words that flip a nearby ``match`` claim. Contractions are listed whole because the
#: tokeniser keeps apostrophes.
_NEGATORS = frozenset(
    {
        "not",
        "never",
        "no",
        "nor",
        "without",
        "fail",
        "fails",
        "failed",
        "failing",
        "cannot",
        "isn't",
        "wasn't",
        "aren't",
        "weren't",
        "doesn't",
        "didn't",
        "don't",
        "can't",
    }
)


def _match_claim_polarity(text: str) -> set[str]:
    """What ``text`` claims about the delivery matching: ``{"asserted"}``, ``{"denied"}``, both, or neither.

    "the buyer said it matched"                       -> {"asserted"}
    "the buyer confirmed the match and returned it"   -> {"asserted"}
    "what arrived does not match what was pitched"    -> {"denied"}
    "the buyer reports a mismatch"                    -> {"denied"}
    "the delivery differed from the pitch"            -> set()   (says nothing about matching)

    Nothing here decides which polarity is CORRECT — the approved manifest does that, and
    the gate below reads it from the manifest rather than typing it as a literal.
    """
    polarity: set[str] = set()
    for claim in _MATCH_CLAIM.finditer(text):
        if claim.group("denied"):
            polarity.add("denied")
            continue
        preceding = re.findall(r"[A-Za-z']+", text[: claim.start()])[-_NEGATION_WINDOW:]
        polarity.add(
            "denied" if any(word.lower() in _NEGATORS for word in preceding) else "asserted"
        )
    return polarity


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

    STRENGTHENED (T-287). This test loaded the manifest's authoritative description and then
    never compared the two: its load-bearing assertion was ``assert "said it matched" not in
    gloss``, which forbids ONE EXACT PHRASE rather than checking agreement. Measured: the
    live gloss is red (correct), but ``"the buyer confirmed the match and returned it"`` —
    the same inverted meaning, reworded — went GREEN. Under ``xfail(strict=True)`` that green
    XPASSes and FORCES the marker's removal, so a reword would have retired T-207 with the
    contradiction fully intact. The original assertion is UNCHANGED and still runs first; the
    comparison added after it derives its expectation from the manifest text already loaded
    above, so only a gloss that actually agrees with the authority passes.

    Note the direction of the derivation, since deriving an expectation from the thing under
    test is its own defect (T-289): the MANIFEST is the authority (SPEC A3) and the GLOSS is
    what is defended. Reading the expectation off the manifest is the point — reword the
    manifest and the expectation must move with it.
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

    approved_polarity = _match_claim_polarity(description)
    assert approved_polarity, (
        f"the manifest's mismatch_return description no longer says anything about whether "
        f"the delivery matched, so this gate has no authority left to compare against: "
        f"{description!r}"
    )
    gloss_polarity = _match_claim_polarity(gloss)
    assert gloss_polarity <= approved_polarity, (
        f"scoring/engine.py's gloss on mismatch_return CONTRADICTS the approved manifest. "
        f"The gloss {gloss.strip()!r} claims the delivery match was {sorted(gloss_polarity)}; "
        f"the manifest {description!r} says it was {sorted(approved_polarity)}. This is a "
        f"comparison against the authority, not a ban on one phrasing: rewording the "
        f"inversion ('the buyer confirmed the match and returned it') does not clear it, and "
        f"only a gloss that agrees with the manifest does. The manifest is the authority "
        f"(SPEC A3) and it decides what the 1.5 weight is applied to."
    )


def test_the_mismatch_return_gloss_gate_is_not_evadable_by_rewording_the_inversion() -> None:
    """The gate above, attacked, against the REAL manifest description.

    The candidate glosses below all keep the inverted meaning and only change the words. Each
    was measured GREEN against ``assert "said it matched" not in gloss`` — and an XPASS under
    ``strict=True`` would have forced T-207's marker off with the contradiction intact. They
    must now be red. The agreeing glosses at the bottom must stay green: a gate that flags a
    correct rewrite is no more useful than one that misses the defect.
    """
    manifest = json.loads((REPO_ROOT / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    behaviours = (manifest.get("dishonest_store") or {}).get("behaviours") or []
    description = next(
        str(row.get("description", ""))
        for row in behaviours
        if isinstance(row, dict) and row.get("type") == "mismatch_return"
    )
    approved = _match_claim_polarity(description)
    assert approved == {"denied"}, (
        f"the approved manifest no longer denies the match, so every expectation below is "
        f"pointed at the wrong authority: {description!r} -> {sorted(approved)}"
    )

    inverted = (
        "``mismatch_return``  1.5  the buyer said it matched and then returned it",
        "``mismatch_return``  1.5  the buyer confirmed the match and returned it",
        "``mismatch_return``  1.5  the delivery matched the pitch and came back anyway",
        "``mismatch_return``  1.5  a matching delivery the buyer returned regardless",
        "``mismatch_return``  1.5  the buyer reported a mismatch, then said it matched",
    )
    slipped = [text for text in inverted if _match_claim_polarity(text) <= approved]
    assert slipped == [], (
        f"these glosses still say the buyer got what was pitched and clear the gate anyway: "
        f"{slipped}. The gate is forbidding a phrase again instead of comparing meanings, "
        f"which is the hole T-287 measured."
    )

    agreeing = (
        "``mismatch_return``  1.5  the buyer reports that what arrived does not match the pitch",
        "``mismatch_return``  1.5  the buyer reports a mismatch and returns the item",
        "``mismatch_return``  1.5  what arrived did not match what was pitched, and went back",
        "``mismatch_return``  1.5  the buyer reports the delivery differed from the pitch",
    )
    refused = [text for text in agreeing if not _match_claim_polarity(text) <= approved]
    assert refused == [], (
        f"these glosses agree with the approved manifest and the gate rejects them anyway: "
        f"{refused}. A gate that is red against its own fix cannot be closed."
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


#: A store whose evidence is uniformly contradicted across every reachable dimension. Enough
#: episodes that the Beta prior cannot hold the score up.
def _dishonest_observations(store_id: str) -> list[dict[str, Any]]:
    return [
        {"store_id": store_id, "dim": dim, "type": "contradicted", "observed_at": AS_OF}
        for dim in (
            "price_honored",
            "discount_honored",
            "shipped_on_time",
            "not_returned",
            "catalog_claim_accuracy",
        )
        for _ in range(30)
    ]


def _store(store_id: str, identity: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"store_id": store_id, "business_identity": identity, "observations": observations}


def test_the_delisting_seam_records_a_decision_the_ledger_can_actually_hold() -> None:
    """The gate above proves the kind is EMITTED somewhere. This proves the event is real.

    A grep for the literal is satisfied by a constant nobody builds an event from, so the
    seam is graded here against the three things that decide whether the decision survives:
    the ledger's own published payload shape for the kind (``contracts.ledger``), the
    writer's envelope validation (``trust.events.normalise_event``), and an actual append
    into a store that hash-chains it.
    """
    from contracts.ledger import LEDGER_PAYLOAD_SHAPES, validate_ledger_payload
    from trust.events import InMemoryEventStore, append, normalise_event
    from trust.scoring import Blacklist
    from trust.snapshot import build_snapshot

    snapshot = build_snapshot(
        [_store("s-bad", "bad-co", _dishonest_observations("s-bad"))],
        blacklist=Blacklist(),
        as_of=AS_OF,
    )
    delistings = snapshot["delistings"]
    assert [event["kind"] for event in delistings] == ["blacklisted"], (
        f"a store scoring {snapshot['stores']['s-bad']['score']} against an empty registry "
        f"must imply exactly one delisting; got {delistings}"
    )

    event = delistings[0]
    assert set(LEDGER_PAYLOAD_SHAPES["blacklisted"]) <= set(event["payload"]), (
        f"the blacklisted payload is missing published keys: "
        f"{sorted(set(LEDGER_PAYLOAD_SHAPES['blacklisted']) - set(event['payload']))}"
    )
    assert validate_ledger_payload("blacklisted", event["payload"]) == []
    assert normalise_event(event)["kind"] == "blacklisted"

    store = InMemoryEventStore()
    outcome = append(store, event)
    assert outcome.inserted and outcome.seq == 1
    assert store.read()[0]["payload"]["reason_code"] == "trust_score_below_threshold"


def test_a_delisting_decision_is_idempotent_and_replayable() -> None:
    """Same snapshot, same events — including the id the ledger deduplicates on (D16)."""
    from trust.events import InMemoryEventStore, append
    from trust.scoring import Blacklist
    from trust.snapshot import build_snapshot

    stores = [_store("s-bad", "bad-co", _dishonest_observations("s-bad"))]
    first = build_snapshot(stores, blacklist=Blacklist(), as_of=AS_OF)["delistings"]
    second = build_snapshot(stores, blacklist=Blacklist(), as_of=AS_OF)["delistings"]
    assert first == second, "the delisting decision is not deterministic, so a replay diverges"

    store = InMemoryEventStore()
    append(store, first[0])
    again = append(store, second[0])
    assert not again.inserted and store.length == 1, (
        "re-running the same snapshot appended a second copy of the same decision; the "
        "event_id is not derived from the decision"
    )


def test_a_store_the_registry_already_blocks_is_not_delisted_twice() -> None:
    """The registry's answer is the state; the threshold only proposes changes to it."""
    from trust.scoring import Blacklist
    from trust.snapshot import build_snapshot

    blacklist = Blacklist()
    blacklist.add(business_identity="bad-co", reason_code="trust_score_below_threshold")
    snapshot = build_snapshot(
        [_store("s-bad", "bad-co", _dishonest_observations("s-bad"))],
        blacklist=blacklist,
        as_of=AS_OF,
    )
    assert snapshot["stores"]["s-bad"]["blacklisted"] is True
    assert snapshot["delistings"] == [], (
        "a store already listed produced a second `blacklisted` event, so every snapshot "
        "would re-delist it"
    )


def test_an_honest_store_is_never_delisted() -> None:
    """The seam has to be silent about the stores it has no case against."""
    from trust.scoring import BLACKLIST_THRESHOLD, Blacklist
    from trust.snapshot import build_snapshot

    honest = [
        {"store_id": "s-ok", "dim": dim, "type": "verified", "observed_at": AS_OF}
        for dim in ("price_honored", "discount_honored", "shipped_on_time")
        for _ in range(30)
    ]
    snapshot = build_snapshot([_store("s-ok", "ok-co", honest)], blacklist=Blacklist(), as_of=AS_OF)
    assert snapshot["stores"]["s-ok"]["score"] >= BLACKLIST_THRESHOLD
    assert snapshot["delistings"] == []


def test_a_lapsed_listing_is_recorded_as_expired_and_the_evidence_re_lists_the_store() -> None:
    """Both halves, because recording only one leaves a hole exactly where an appeal looks."""
    from trust.scoring import Blacklist
    from trust.snapshot import build_snapshot

    blacklist = Blacklist()
    blacklist.add(
        business_identity="bad-co",
        reason_code="manual_review",
        expires_at="2025-01-01T00:00:00Z",
    )
    snapshot = build_snapshot(
        [_store("s-bad", "bad-co", _dishonest_observations("s-bad"))],
        blacklist=blacklist,
        as_of=AS_OF,
    )
    kinds = [event["kind"] for event in snapshot["delistings"]]
    assert kinds == ["blacklist_expired", "blacklisted"], (
        f"a listing that lapsed while the score is still sub-threshold must record BOTH the "
        f"expiry and the fresh delisting; got {kinds}"
    )

    expired, delisted = snapshot["delistings"]
    assert expired["payload"]["reason_code"] == "manual_review", (
        "the expiry event must carry the reason the listing was OPENED with; stamping the "
        "trust-score reason on a listing a human opened files a false record of why it ended"
    )
    assert delisted["payload"]["reason_code"] == "trust_score_below_threshold"


def test_a_registry_that_cannot_answer_never_releases_a_store() -> None:
    """Fail closed, in the same direction as ``is_blacklisted`` on the same inputs."""
    from trust.snapshot import build_snapshot
    from trust.snapshot.delisting import delisting_events

    class _Unavailable:
        def lookup(self, business_identity: str) -> Any:
            raise RuntimeError("blacklist source is down")

    snapshot = build_snapshot(
        [_store("s-bad", "bad-co", _dishonest_observations("s-bad"))],
        blacklist=_Unavailable(),
        as_of=AS_OF,
    )
    assert snapshot["stores"]["s-bad"]["blacklisted"] is True, (
        "is_blacklisted stopped failing closed"
    )
    assert snapshot["delistings"] == [], (
        "a registry that cannot answer produced a `blacklist_expired` event, which would "
        "release a listed store because its source was down"
    )
    assert delisting_events([], blacklist=_Unavailable(), as_of=AS_OF) == []


def test_the_published_threshold_is_read_by_product_code_and_not_only_by_tests() -> None:
    """T-237's other half: the constant had zero product readers outside its own definition.

    Derived from the source rather than asserted as a path list, so moving the reader keeps
    this green and DELETING every reader turns it red.
    """
    readers = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in _product_python_files()
        if "BLACKLIST_THRESHOLD" in path.read_text(encoding="utf-8", errors="ignore")
        and path.name not in {"engine.py", "__init__.py"}
    )
    assert readers, (
        "BLACKLIST_THRESHOLD is exported and compared only in tests again — the published "
        "threshold decides nothing until product code reads it (S2)"
    )


def test_the_threshold_comparison_is_strict_and_a_score_exactly_at_it_stays_listed() -> None:
    """T-237's boundary, which no gate above pins: ``<`` and not ``<=``.

    ``trust.scoring``'s own export line calls ``BLACKLIST_THRESHOLD`` "the published score
    BELOW which a store is out", and :func:`below_blacklist_threshold` commits in its
    docstring to "Strict ``<``". Nothing graded it. Every other T-237 gate scores a store
    either uniformly contradicted (far below the floor) or uniformly verified (far above
    it), so relaxing the comparison to ``<=`` leaves all nine of them green while it
    delists a store sitting exactly ON the published floor -- a store the manifest says is
    IN. The two neighbours are asserted as well, so this cannot be satisfied by a function
    that simply refuses every float.
    """
    import math

    from trust.scoring import BLACKLIST_THRESHOLD
    from trust.snapshot.delisting import below_blacklist_threshold

    assert below_blacklist_threshold(BLACKLIST_THRESHOLD) is False, (
        f"a store scoring exactly the published floor ({BLACKLIST_THRESHOLD}) was read as "
        f"sub-threshold; the comparison is `<=` where the published reading is strict `<`, "
        f"so the floor itself is now a delisting"
    )
    assert below_blacklist_threshold(math.nextafter(BLACKLIST_THRESHOLD, 0.0)) is True, (
        "the largest float below the floor was not read as sub-threshold, so the "
        "comparison has moved off the published threshold entirely"
    )
    assert below_blacklist_threshold(math.nextafter(BLACKLIST_THRESHOLD, 1.0)) is False


def test_the_expiry_decision_is_an_event_the_ledger_can_actually_hold_too() -> None:
    """The ``blacklisted`` half is graded against ``contracts.ledger``; the expiry half was not.

    ``LEDGER_PAYLOAD_SHAPES["blacklist_expired"]`` publishes ``("store_id", "reason_code")``.
    The lapsed-listing gate above reads only ``payload["reason_code"]``, so dropping
    ``store_id`` from the expiry payload -- or emitting an envelope the writer refuses --
    turned nothing red, and the event would have failed at the ledger rather than in the
    suite. Same three checks the delisting half already gets: the published payload shape,
    the writer's envelope validation, and a real append.
    """
    from contracts.ledger import LEDGER_PAYLOAD_SHAPES, validate_ledger_payload
    from trust.events import InMemoryEventStore, append, normalise_event
    from trust.scoring import Blacklist
    from trust.snapshot import build_snapshot

    blacklist = Blacklist()
    blacklist.add(
        business_identity="bad-co",
        reason_code="manual_review",
        expires_at="2025-01-01T00:00:00Z",
    )
    snapshot = build_snapshot(
        [_store("s-bad", "bad-co", _dishonest_observations("s-bad"))],
        blacklist=blacklist,
        as_of=AS_OF,
    )
    expired = [event for event in snapshot["delistings"] if event["kind"] == "blacklist_expired"]
    assert len(expired) == 1, (
        f"a lapsed listing must record exactly one expiry; got "
        f"{[event['kind'] for event in snapshot['delistings']]}"
    )

    event = expired[0]
    assert set(LEDGER_PAYLOAD_SHAPES["blacklist_expired"]) <= set(event["payload"]), (
        f"the blacklist_expired payload is missing published keys: "
        f"{sorted(set(LEDGER_PAYLOAD_SHAPES['blacklist_expired']) - set(event['payload']))}"
    )
    assert validate_ledger_payload("blacklist_expired", event["payload"]) == []
    assert normalise_event(event)["kind"] == "blacklist_expired"

    store = InMemoryEventStore()
    outcome = append(store, event)
    assert outcome.inserted and outcome.seq == 1
    written = store.read()[0]["payload"]
    assert written["store_id"] == "s-bad", (
        "the expiry event does not name the store it releases, so a reader cannot tell "
        "WHICH listing lapsed"
    )
    assert written["reason_code"] == "manual_review"


def test_the_delisting_seam_itself_compares_a_score_to_the_published_threshold() -> None:
    """The gate above greps the file TEXT of the WHOLE tree, and both halves of that leak.

    Measured, which is why this exists rather than a looser sibling:

    * **Prose satisfies a text grep.** ``delisting.py`` names ``BLACKLIST_THRESHOLD`` in its
      module docstring as well as in its code, so gutting the module to comments leaves the
      substring gate green.
    * **Another module holds the whole-tree gate green.** ``services/sim/src/runner.py``
      loads the constant independently, so *any* whole-tree "somebody reads it" assertion --
      substring or AST -- stays green with every reader inside the trust app deleted.
    * **Not every load is a decision.** ``delisting.py`` also puts the constant in a payload
      (``"threshold": BLACKLIST_THRESHOLD``), so an "is it loaded here" check stays green with
      the one comparison that actually delists a store removed.

    So this asks the narrowest true question: the module that EMITS the delisting must use
    the published threshold as an operand of a COMPARISON. The module is resolved through the
    product's own import, not a hard-coded path, so moving the seam keeps this green and
    hollowing it out turns it red.
    """
    from trust.snapshot import delisting

    source_path = pathlib.Path(delisting.__file__ or "")
    assert source_path.is_file(), f"the delisting seam has no readable source at {source_path}"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    def _names_the_threshold(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "BLACKLIST_THRESHOLD"
        return isinstance(node, ast.Attribute) and node.attr == "BLACKLIST_THRESHOLD"

    comparisons = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(_names_the_threshold(side) for side in (node.left, *node.comparators))
    )

    assert comparisons, (
        f"{source_path.name} emits the delisting decision but never COMPARES anything to "
        f"BLACKLIST_THRESHOLD -- every remaining occurrence is a docstring, a comment, an "
        f"unused import, or a payload field that reports the threshold without applying it. "
        f"The published threshold decides nothing until the seam compares a score to it (S2), "
        f"and neither a text grep nor a whole-tree 'somebody loads it' check can see that: "
        f"services/sim/src/runner.py loads it independently and would hold both green"
    )
