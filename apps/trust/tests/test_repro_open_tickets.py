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


# ======================================================================================
# T-172 — an undeclared docker test is silently widened to the whole stack
# ======================================================================================
#: The plugin the collection subprocess below runs under. Written to ``tmp_path`` and loaded
#: with ``-p`` rather than imported, because it must observe the REAL suite's collection —
#: the same ``pytest_collection_modifyitems`` seam ``conftest.py`` classifies items in — and
#: ``trylast`` so it sees the item list AFTER ``-m docker`` has deselected everything else.
_T172_COLLECTOR = '''\
import json, os, pathlib
import pytest
from proxyshop_support import service_markers

_RECORDS = []


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items):
    for item in items:
        marker_args = [list(mark.args) for mark in item.iter_markers("docker")]
        fixtures = sorted(set(item.fixturenames) & set(service_markers.FIXTURE_SERVICES))
        try:
            services = list(service_markers.services_for(marker_args, item.fixturenames))
            refusal = None
        except Exception as exc:  # a LOUD resolution is a fix, not a defect
            services, refusal = [], f"{type(exc).__name__}: {exc}"
        _RECORDS.append(
            {
                "nodeid": item.nodeid,
                "marker_args": marker_args,
                "mapped_fixtures": fixtures,
                "services": services,
                "refusal": refusal,
            }
        )


def pytest_sessionfinish(session, exitstatus):
    pathlib.Path(os.environ["T172_CORPUS_OUT"]).write_text(
        json.dumps(_RECORDS), encoding="utf-8"
    )
'''

#: Substrings that betray, in a test module's own source, that it talks to a given compose
#: service. Deliberately generous — the point is that a module mentioning NONE of a service's
#: spellings anywhere cannot possibly need that service, which is ground truth INDEPENDENT of
#: ``services_for`` and of ``FIXTURE_SERVICES``. Keyed by service so a new compose service
#: makes the armed guard below fail rather than silently drop out of the sweep.
_T172_SERVICE_EVIDENCE: dict[str, str] = {
    "postgres": r"postgres|psycopg|\bpg_|libpq",
    "redis": r"redis",
    "neo4j-bolt": r"neo4j|bolt",
}

#: The smallest docker corpus this gate will reason about. Measured at 264; a collection that
#: silently drops most of the suite must fail rather than pass over the remainder.
_T172_MIN_DOCKER_ITEMS = 200


#: One collection per session, shared by the armed guard and the repro. Collecting the whole
#: suite twice is ~5s of pure duplication in ``make verify``.
_T172_CORPUS_CACHE: list[dict[str, Any]] | None = None


def _t172_collect_docker_corpus(tmp_path: Any) -> list[dict[str, Any]]:
    """Every ``-m docker`` item the real suite collects, with the services it resolves to.

    A subprocess running the repo's own collection, not a re-implementation of it: the
    defect lives in what ``conftest.py`` asks ``services_for`` at collection time, so
    anything short of a real collection grades a model of the rule instead of the rule.
    """
    global _T172_CORPUS_CACHE
    if _T172_CORPUS_CACHE is not None:
        return _T172_CORPUS_CACHE

    plugin_dir = tmp_path / "t172_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "t172_corpus.py").write_text(_T172_COLLECTOR, encoding="utf-8")
    out = tmp_path / "t172_corpus.json"

    env = dict(os.environ)
    env["T172_CORPUS_OUT"] = str(out)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                "docker",
                "-p",
                "no:cacheprovider",
                "-p",
                "t172_corpus",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired as expiry:  # pragma: no cover - a hang is a third outcome
        raise AssertionError(
            "collecting the docker corpus did not finish in 240s. A gate that hangs prints "
            "no red at all, so this is a failure and not a slow pass."
        ) from expiry

    assert completed.returncode == 0, (
        f"collecting `-m docker` failed with rc={completed.returncode}; this gate cannot "
        f"reason about a corpus it could not build.\nstdout tail:\n"
        f"{completed.stdout[-2000:]}\nstderr tail:\n{completed.stderr[-2000:]}"
    )
    assert out.is_file(), (
        "the collection subprocess exited 0 but wrote no corpus, so the collector plugin "
        "never ran — every count below would have been silently zero"
    )
    records: list[dict[str, Any]] = json.loads(out.read_text(encoding="utf-8"))
    _T172_CORPUS_CACHE = records
    return records


def _t172_declares_a_service(record: dict[str, Any]) -> bool:
    """True when the item said what it needs, by either route the rule reads."""
    return bool(any(record["marker_args"]) or record["mapped_fixtures"])


def _t172_module_evidence(path: str, cache: dict[str, set[str]]) -> set[str]:
    """The services a test module's OWN SOURCE shows any sign of talking to.

    Ground truth for "could this test possibly need Redis?" that never consults
    ``services_for`` or ``FIXTURE_SERVICES`` — so the consequence layer below is not the
    structural layer restated, and cannot be satisfied by teaching the rule a new fixture.
    """
    if path not in cache:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        cache[path] = {
            service
            for service, pattern in _T172_SERVICE_EVIDENCE.items()
            if re.search(pattern, source, re.IGNORECASE)
        }
    return cache[path]


def test_t172_the_docker_corpus_sweep_is_armed(tmp_path: Any) -> None:
    """Before anything is concluded about the corpus, prove there IS a corpus.

    Not xfail: this must hold today and must keep holding after the fix. Three sweeps in this
    repo have gone quiet rather than red (6 -> 0 of 8, 70 -> 0 of 79, 48 -> 0 of 66), and the
    gate below is a walk over collected items — the cheapest way to silence it is a collection
    that finds nothing.

    It also pins the two things the consequence layer depends on: that
    :data:`_T172_SERVICE_EVIDENCE` still covers every compose service (a new service would
    otherwise drop out of the sweep unnoticed), and that the evidence reader actually
    discriminates — a module with narrow evidence must read narrow.
    """
    from proxyshop_support import reachability

    assert set(_T172_SERVICE_EVIDENCE) == set(reachability.SERVICES), (
        f"the module-evidence table covers {sorted(_T172_SERVICE_EVIDENCE)} but compose now "
        f"runs {sorted(reachability.SERVICES)}. An uncovered service silently reads as 'no "
        f"evidence anywhere', which would make the consequence layer below fire on every "
        f"item, or — with the difference the other way — never fire at all"
    )

    records = _t172_collect_docker_corpus(tmp_path)
    assert len(records) >= _T172_MIN_DOCKER_ITEMS, (
        f"only {len(records)} `-m docker` items collected, below the {_T172_MIN_DOCKER_ITEMS} "
        f"floor (264 at the time this was written). The gate below walks this list, so a "
        f"shrunken corpus would let it pass by having nothing left to look at"
    )
    declaring = [record for record in records if _t172_declares_a_service(record)]
    assert len(declaring) >= _T172_MIN_DOCKER_ITEMS // 2, (
        f"only {len(declaring)} of {len(records)} docker items declare a service at all. The "
        f"structural layer below asks every item to declare one; if almost none do, that "
        f"layer is grading a corpus that never worked rather than a regression"
    )

    cache: dict[str, set[str]] = {}
    narrow = [
        record
        for record in records
        if _t172_module_evidence(record["nodeid"].split("::")[0], cache)
        != set(reachability.SERVICES)
    ]
    assert len(narrow) >= _T172_MIN_DOCKER_ITEMS, (
        f"only {len(narrow)} of {len(records)} docker items live in a module whose source "
        f"shows evidence for FEWER than all {len(reachability.SERVICES)} services (250 when "
        f"this was written). The consequence layer can only fire on those, so if the evidence "
        f"reader started answering 'all three' everywhere it would pass vacuously"
    )

    canary = {
        service
        for service, pattern in _T172_SERVICE_EVIDENCE.items()
        if re.search(pattern, "import psycopg\n", re.IGNORECASE)
    }
    assert canary == {"postgres"}, (
        f"the evidence reader answered {sorted(canary)} for a module whose entire source is "
        f"`import psycopg`. It is meant to read narrow for a narrow module; if it reads wide "
        f"the consequence layer below can never fire"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-172: service_markers.services_for widens an item that declares no service to the "
        "whole stack, so 8 Postgres-only docker tests — 5 schema-grants, 2 role-password, 1 "
        "closed-loopback — are skipped at exit 0 by a Redis-only or neo4j-only outage; remove "
        "this marker with the fix"
    ),
)
def test_t172_an_undeclared_docker_test_is_widened_to_the_whole_stack(tmp_path: Any) -> None:
    """A test that needs one store must not be silenced by a different store being down.

    Three assertions that fail for three DIFFERENT reasons, so no single edit closes the gate
    while leaving the defect alive:

    * **the corpus** — every collected docker item must say what it needs. This is the hole
      the ticket counted: 8 items say nothing and are handed all three services.
    * **the branch the ticket scopes** (``service_markers.py``'s ``if not named: return
      reachability.SERVICES``) — "I declared nothing" must be distinguishable from "I declared
      everything". Annotating the 8 tests makes the fallback unreachable from the corpus,
      which would leave the branch free to be reverted to anything with the first assertion
      still green; this one stays red until the branch itself stops silently widening.
      Either shape closes it — a refusal, or an answer that is not the whole stack — because
      prescribing one would be grading an implementation rather than the property.
    * **the consequence**, read off ground truth that is not ``services_for``: a module whose
      own source never mentions Redis in any spelling cannot need Redis, so no item in it may
      be gated on Redis. This is what makes the gate survive its own fix — it cannot be
      satisfied by teaching ``FIXTURE_SERVICES`` a new fixture, nor by stamping
      ``@pytest.mark.docker("postgres", "neo4j-bolt", "redis")`` on the offenders, both of
      which close the first assertion while leaving every named test just as skippable.
    """
    from proxyshop_support import reachability, service_markers

    records = _t172_collect_docker_corpus(tmp_path)
    assert records, "no docker items collected; see the armed-sweep guard above"

    undeclared = [
        record
        for record in records
        if not _t172_declares_a_service(record) and record["refusal"] is None
    ]
    assert undeclared == [], (
        f"{len(undeclared)} of {len(records)} `-m docker` items declare no service — no "
        f"marker argument and no fixture in FIXTURE_SERVICES — so services_for falls through "
        f"to `return reachability.SERVICES` and every one of them is skipped at exit 0 by a "
        f"single-store outage it does not depend on: "
        f"{[record['nodeid'] for record in undeclared]}"
    )

    everything = [tuple(reachability.SERVICES)]
    declared_whole_stack = service_markers.services_for(everything, ())
    try:
        widened: tuple[str, ...] | None = service_markers.services_for([], ())
    except Exception:
        widened = None  # a loud refusal is a fix: nothing is silently widened
    assert widened is None or tuple(widened) != tuple(declared_whole_stack), (
        f"services_for answers {list(declared_whole_stack)} for an item that declared the "
        f"whole stack AND for an item that declared nothing at all, so the two are "
        f"indistinguishable downstream and conftest skips them identically. The ticket scopes "
        f"this branch (proxyshop_support/service_markers.py, `if not named: return "
        f"reachability.SERVICES`): silence about a dependency must not read as depending on "
        f"everything. Either shape closes this — raise, or answer something other than the "
        f"full stack — but the answers must differ"
    )

    cache: dict[str, set[str]] = {}
    over_gated: list[str] = []
    for record in records:
        path = record["nodeid"].split("::")[0]
        evidence = _t172_module_evidence(path, cache)
        surplus = sorted(set(record["services"]) - evidence)
        if surplus:
            over_gated.append(
                f"{record['nodeid']} gated on {surplus} with module evidence only for "
                f"{sorted(evidence)}"
            )
    assert over_gated == [], (
        f"{len(over_gated)} docker items are gated on a service their own test module never "
        f"mentions in any spelling, so a store they cannot possibly use still skips them at "
        f"exit 0 — the pre-T-109 defect, surviving for exactly these items:\n  "
        + "\n  ".join(over_gated)
    )


# ======================================================================================
# T-259 — trust emits a trust event the store agent's real intake refuses
# ======================================================================================
#: Identity key names planted in the generated event payloads. Every one is an EXACT member
#: of ``trust.feedback.IDENTITY_KEYS`` or carries a member of ``IDENTITY_KEY_SUBSTRINGS``, so
#: ``scrub_report`` removes the KEY and the count it reports is exactly how many were planted
#: — which is what lets the survival assertion below compare against the generated input
#: rather than against a constant.
_T259_IDENTITY_KEYS = (
    "buyer_email",
    "customer_name",
    "shipping_address",
    "phone",
    "user_id",
    "buyer_name",
)

#: The six values trust puts on the wire that ``contracts.TrustEventPayload`` has no room for
#: today. A fix that DELETES them validates just as well as a fix that relocates them, and the
#: deleted version is strictly worse — ``redacted_fields`` is the mechanism trust's own
#: docstring says exists "so the receiving agent (and an auditor) see that a scrub happened
#: without learning what it removed". So the gate asserts they arrive, wherever they are
#: parked, and never asserts the address they are parked at.
_T259_CARRIERS = (
    "schema_version",
    "reason_code",
    "order_ref",
    "identity_disclosed",
    "redacted_fields",
    "policy",
)

#: The pinned half of the generator's seed. XORed with fresh OS entropy on every run.
_T259_PINNED_SEED = 0x259

#: How many deltas the property draws. Large enough to cross every dimension and every ledger
#: kind several times over, small enough that the whole sweep is well under a second.
_T259_CASES = 60


def _t259_rng() -> Any:
    """A generator that is reproducible in its SHAPE and never in its DRAWS.

    A pinned seed alone turns a "randomized property" into a parametrized table wearing a
    costume: the draws become enumerable, and a fix can be tuned to exactly the values that
    seed produces. The pinned half keeps the spread of shapes stable enough to assert on; the
    ``SystemRandom`` half means no run can be pre-satisfied.
    """
    import random

    return random.Random(_T259_PINNED_SEED ^ random.SystemRandom().getrandbits(48))


def _t259_plant(rng: Any) -> tuple[dict[str, Any], int]:
    """One generated event payload, plus how many identity KEYS were planted in it.

    Shape is drawn, not just values: how many identity keys, how deep they are buried, and
    whether the innocent fields sit beside them or below them all vary per case, so a fix that
    only handles the flat single-key case fails most of the sweep.
    """
    planted = rng.sample(_T259_IDENTITY_KEYS, rng.randint(0, 3))
    payload: dict[str, Any] = {"note": "delivered", "sku": f"sku-{rng.randrange(10_000)}"}
    remaining = list(planted)
    node = payload
    for _ in range(rng.randint(0, 2)):
        child: dict[str, Any] = {"level": rng.randrange(10)}
        if remaining and rng.random() < 0.7:
            child[remaining.pop()] = "someone@example.com"
        node["detail"] = child
        node = child
    for key in remaining:
        payload[key] = "someone@example.com"
    return payload, len(planted)


def _t259_cases() -> list[dict[str, Any]]:
    """``_T259_CASES`` trust deltas spanning every dimension and every ledger event kind."""
    from contracts import LedgerEventKind, TrustDimension

    rng = _t259_rng()
    dims = list(TrustDimension)
    kinds = list(LedgerEventKind)
    cases: list[dict[str, Any]] = []
    for index in range(_T259_CASES):
        payload, planted = _t259_plant(rng)
        store_id = f"store-{index}-{rng.randrange(10**9)}"
        cases.append(
            {
                "store_id": store_id,
                "dim": dims[index % len(dims)].value,
                "delta": rng.choice((-1.0, 1.0)) * rng.uniform(0.05, 5.0),
                "order_ref": rng.choice((None, f"order-{index}")),
                "reason_code": rng.choice((None, "mismatch_return", "late_dispatch")),
                "buyer_pseudonym": f"pseudonym-{index}",
                "event": {
                    "event_id": f"event-{index}",
                    "ts": AS_OF,
                    "kind": kinds[index % len(kinds)].value,
                    "store_id": store_id,
                    "payload": payload,
                },
                "planted_identity_keys": planted,
            }
        )
    return cases


def _t259_values_by_key(node: Any, found: dict[str, list[Any]] | None = None) -> dict[str, list[Any]]:
    """Every ``key -> values`` pair anywhere in a nested mapping, at any depth.

    The gate reads the carriers back through this rather than at a pinned address, so
    relocating them into the open ``event.payload`` mapping — the realistic fix, since
    ``packages/contracts`` is frozen — passes, and dropping them does not.
    """
    found = {} if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            found.setdefault(str(key), []).append(value)
            _t259_values_by_key(value, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _t259_values_by_key(value, found)
    return found


def test_t259_the_trust_event_generator_is_armed() -> None:
    """The generator cannot be narrowed into a probe without this going red.

    Not xfail, deliberately. Every assertion inside a ``strict`` xfail test is invisible in a
    normal run — a failure there IS the expected outcome — so a canary placed inside the repro
    below would let a silently narrowed generator sail through ``make verify`` while
    resurrecting exactly the single-value keys the property exists to close. This is the
    canary, and it must pass today and keep passing after the fix.
    """
    from contracts import LedgerEventKind, TrustDimension

    cases = _t259_cases()
    assert len(cases) == _T259_CASES, (
        f"the generator produced {len(cases)} cases, not {_T259_CASES}; a sweep that shrinks "
        f"is a sweep that stops proving anything"
    )
    assert len({case["store_id"] for case in cases}) == _T259_CASES, (
        "two generated cases share a store_id, so the sweep is narrower than it counts"
    )
    assert {case["dim"] for case in cases} == {dim.value for dim in TrustDimension}, (
        f"the generated dims cover only {sorted({case['dim'] for case in cases})} of "
        f"{sorted(dim.value for dim in TrustDimension)} — a property that never varies the "
        f"dimension cannot catch a fix that hard-codes one"
    )
    assert {case["event"]["kind"] for case in cases} == {
        kind.value for kind in LedgerEventKind
    }, "the generated ledger kinds no longer cover the whole frozen vocabulary"

    planted = {case["planted_identity_keys"] for case in cases}
    assert len(planted) >= 3 and 0 in planted and max(planted) >= 2, (
        f"the generator planted only {sorted(planted)} identity keys across {_T259_CASES} "
        f"cases. The redaction-report assertion compares `redacted_fields` against this "
        f"number, so a generator that always plants the same count grades a constant"
    )
    depths = {json.dumps(case["event"]["payload"]).count('"detail"') for case in cases}
    assert len(depths) >= 2, (
        f"every generated payload nests to the same depth ({sorted(depths)}); the scrub walks "
        f"recursively and a single-depth sweep never exercises that"
    )
    assert len({case["reason_code"] for case in cases}) >= 2, (
        "reason_code never varies, so its round-trip assertion below grades a constant"
    )
    assert len({case["order_ref"] is None for case in cases}) == 2, (
        "order_ref is either always present or always absent across the sweep"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-259: trust.feedback.trust_event_payload emits `schema_version`, `reason_code` and a "
        "pseudonymous_context carrying `order_ref`/`identity_disclosed`/`redacted_fields`/"
        "`policy`, and contracts.TrustEventPayload forbids extras — so the store agent's real "
        "intake rejects 60 of 60 emitted events while both suites stay green against their own "
        "doubles; remove this marker with the fix"
    ),
)
def test_t259_every_trust_event_trust_emits_is_ingestible_by_the_store_agent() -> None:
    """The two ends of R13 must agree, and the agreement must be measured across the seam.

    ``apps/trust`` pushes into a local ``_RecordingSink``; ``packages/store-agent`` ingests a
    hand-written dict its own fixtures call "the frozen suite's TrustEventPayload, shape for
    shape". Nothing joins the two, and ``apps/trust`` never validates its own emission against
    ``contracts.TrustEventPayload`` — the model the intake side really does validate with. So
    a field-name drift on either side is invisible to both suites while both stay green, which
    is precisely what has happened.

    Two assertions, because acceptance alone rewards the wrong fix. The cheapest way to make
    the intake stop refusing is to DELETE the six fields it has no room for; that validates
    perfectly and silently drops the redaction report an auditor needs. So the second
    assertion reads the six carriers back off the payload the intake actually accepted,
    wherever the fix parks them, and compares them to each case's own generated inputs.
    """
    from store_agent.modes import AgentRunner
    from trust.feedback import TRUST_EVENT_SCHEMA_VERSION, trust_event_payload

    cases = _t259_cases()
    assert len(cases) == _T259_CASES, "the generator is not armed; see the guard above"

    rejected: list[tuple[str, str, str]] = []
    accepted: list[tuple[dict[str, Any], Any, Any]] = []
    for case in cases:
        emitted = trust_event_payload(case)
        runner = AgentRunner(
            {"store_id": case["store_id"]}, sink=object(), submitter=None, mode="shadow"
        )
        try:
            validated = runner.ingest_trust_event(emitted)
        except Exception as refusal:
            rejected.append(
                (case["dim"], type(refusal).__name__, str(refusal).splitlines()[0])
            )
        else:
            accepted.append((case, validated, runner.trust_posture))

    assert rejected == [], (
        f"{len(rejected)} of {len(cases)} events trust emits are refused by the store agent's "
        f"real intake (store_agent.modes.AgentRunner.ingest_trust_event, which validates with "
        f"contracts.TrustEventPayload). First refusal: {rejected[0]}. Both suites stay green "
        f"because trust only ever pushes into a local _RecordingSink and the store agent only "
        f"ever ingests its own hand-written dict, so nothing joins the emitted payload to the "
        f"model the intake really parses"
    )

    losses: list[str] = []
    for case, validated, posture in accepted:
        dumped = validated.model_dump(mode="json")
        seen = _t259_values_by_key(dumped)

        signals = {signal.dim: signal for signal in posture.signals}
        landed = signals.get(case["dim"]) or next(
            (signal for signal in posture.signals if str(signal.dim) == case["dim"]), None
        )
        if landed is None or abs(float(landed.net_delta) - case["delta"]) > 1e-9:
            losses.append(
                f"{case['store_id']}: the delta {case['delta']!r} did not land on "
                f"{case['dim']!r} — posture reads {[(str(s.dim), s.net_delta) for s in posture.signals]}"
            )
            continue

        for carrier in _T259_CARRIERS:
            if carrier not in seen:
                losses.append(f"{case['store_id']}: {carrier!r} is nowhere on the ingested payload")
        if "redacted_fields" in seen and case["planted_identity_keys"] not in {
            value for value in seen["redacted_fields"] if isinstance(value, int)
        }:
            losses.append(
                f"{case['store_id']}: redacted_fields reads {seen['redacted_fields']} but "
                f"{case['planted_identity_keys']} identity keys were planted"
            )
        if "identity_disclosed" in seen and any(value for value in seen["identity_disclosed"]):
            losses.append(f"{case['store_id']}: identity_disclosed is truthy on the wire")
        if "schema_version" in seen and TRUST_EVENT_SCHEMA_VERSION not in seen["schema_version"]:
            losses.append(f"{case['store_id']}: schema_version does not round-trip")
        if case["reason_code"] is not None and case["reason_code"] not in seen.get(
            "reason_code", []
        ):
            losses.append(f"{case['store_id']}: reason_code {case['reason_code']!r} was dropped")
        if case["order_ref"] is not None and case["order_ref"] not in seen.get("order_ref", []):
            losses.append(f"{case['store_id']}: order_ref {case['order_ref']!r} was dropped")

    assert losses == [], (
        f"{len(losses)} losses across {len(accepted)} accepted events. The intake accepted the "
        f"payload, but the values trust put on the wire did not survive the crossing — which "
        f"is what a fix that simply DELETES the six fields contracts has no room for looks "
        f"like. redacted_fields in particular is the only signal a store agent or an auditor "
        f"gets that a scrub happened at all.\n  " + "\n  ".join(losses[:12])
    )


# ======================================================================================
# T-303 — T-237's remaining halves: the decision is dropped, and nobody asks for it
# ======================================================================================
#: How many unknown store ids the fail-closed canary draws. A wiring that answers ELIGIBLE for
#: a store it has never heard of is worse than no wiring at all, so this must pass today AND
#: keep passing through whatever fix lands.
_T303_UNKNOWN_STORES = 60


def _t303_simulation_run() -> Any:
    """One whole S2 run of the only product caller of ``build_snapshot``.

    Datastore-free, socket-free, clock-free and seeded: ``services/sim/src/runner.py`` states
    that in its own header and the repo's ``pytest-socket`` guard is armed while this runs.
    Measured at ~0.02s warm, so this is a behavioural gate and not a slow one.
    """
    from fixtures.manifest import load_manifest
    from sim.runner import run_simulation

    manifest = load_manifest()
    return run_simulation(manifest, int(manifest["seed"]))


def _t303_auction_body(store_ids: Any) -> dict[str, Any]:
    """A roster request built from the route's own model, not hand-written JSON.

    ``list_price`` is required and ``store_id`` has a length bound; a body that 422s would
    make the gate below error on its own request shape rather than on the substance.
    """
    return {
        "intent": {"intent_id": "intent-t303", "cluster_id": "cluster-t303"},
        "roster": [
            {"store_id": str(store_id), "tier": 1, "list_price": 19.99}
            for store_id in store_ids
        ],
    }


def test_t303_the_delisting_and_eligibility_sweeps_are_armed() -> None:
    """Both halves below walk something. Prove there is something to walk.

    Not xfail. The first half asserts about the delistings a real run computes and the events
    it sealed; the second asserts about the stores a served auction answered for. Either
    becomes a vacuous pass the moment its list is empty — which is how three sweeps in this
    repo went quiet instead of red — so the counts are pinned here, outside the xfail.
    """
    from fastapi.testclient import TestClient
    from trust.scoring import BLACKLIST_THRESHOLD

    run = _t303_simulation_run()
    assert run.chain_ok, "the simulation's own event chain does not verify; nothing below is trustworthy"
    assert len(run.events) >= 20, (
        f"the simulation sealed only {len(run.events)} events (36 when this was written). The "
        f"first half below looks for a delisting INSIDE this stream; a stream this short "
        f"means the run stopped doing the thing being graded"
    )
    delistings = run.snapshot["delistings"]
    assert len(delistings) >= 1, (
        f"the run computed {len(delistings)} delistings, so there is no dropped decision left "
        f"to notice. Either the dishonest store stopped scoring below BLACKLIST_THRESHOLD "
        f"({BLACKLIST_THRESHOLD}) or the delisting seam stopped emitting — both make the gate "
        f"below pass while saying nothing"
    )
    scores = {
        store_id: entry["score"] for store_id, entry in run.snapshot["stores"].items()
    }
    assert any(score < BLACKLIST_THRESHOLD for score in scores.values()), (
        f"no store in the run scores below the published threshold {BLACKLIST_THRESHOLD}; "
        f"scores were {scores}. S2 has nothing to catch"
    )
    assert any(score >= BLACKLIST_THRESHOLD for score in scores.values()), (
        f"every store in the run scores below {BLACKLIST_THRESHOLD}, so 'an honest store is "
        f"not delisted' is not being exercised by anything; scores were {scores}"
    )

    from exchange.main import create_app

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/auctions", json=_t303_auction_body(("store-a", "store-b")))
    assert response.status_code == 201, (
        f"POST /auctions answered {response.status_code}, not 201: {response.text[:400]}. The "
        f"second half below reads this body, so a request the route refuses would make it "
        f"error on its own shape instead of on the wiring"
    )
    body = response.json()
    assert len(body["solicited"]) + len(body["denied"]) == 2, (
        f"the served auction accounted for {len(body['solicited'])} solicited + "
        f"{len(body['denied'])} denied out of a 2-store roster, so the gate below would be "
        f"reading a partial answer"
    )


def test_t303_the_exchange_eligibility_gate_never_fails_open() -> None:
    """Whatever the exchange ends up asking, it must not admit a store it never heard of.

    Not xfail: this passes today and must keep passing through the fix. It is the property
    that stops the obvious wrong way to green the repro below — wiring a source whose default
    is ELIGIBLE, or one that swallows its backend being down. ``read_eligibility`` is the
    repo's own fail-closed reader, so this grades the rule the gates actually apply rather
    than a re-derivation of it.
    """
    import random

    from exchange.eligibility import ELIGIBLE, read_eligibility
    from exchange.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        client.post("/auctions", json=_t303_auction_body(("store-warmup",)))
    source = getattr(app.state, "seller_eligibility", None)
    assert source is not None, (
        "the served exchange exposes no seller_eligibility after answering an auction, so "
        "there is nothing to probe — which is itself the wiring gap T-303 names"
    )

    rng = random.Random(0x303 ^ random.SystemRandom().getrandbits(48))
    unknown = [f"store-unknown-{rng.randrange(10**12)}" for _ in range(_T303_UNKNOWN_STORES)]
    assert len(set(unknown)) == _T303_UNKNOWN_STORES, "the unknown-store draw collided"

    admitted: list[str] = []
    raised: list[str] = []
    for store_id in unknown:
        try:
            decision = read_eligibility(source, store_id)
        except Exception as failure:  # a raising source must be read as a denial, not escape
            raised.append(f"{store_id}: {type(failure).__name__}: {failure}")
            continue
        if decision.status == ELIGIBLE or decision.eligible:
            admitted.append(store_id)

    assert raised == [], (
        f"{len(raised)} of {_T303_UNKNOWN_STORES} eligibility reads escaped as exceptions "
        f"instead of denying; read_eligibility exists so a backend that is down denies rather "
        f"than throwing.\n  " + "\n  ".join(raised[:5])
    )
    assert admitted == [], (
        f"the wired eligibility source admitted {len(admitted)} of {_T303_UNKNOWN_STORES} "
        f"store ids it has never heard of, e.g. {admitted[:3]}. A source that says yes by "
        f"default is indistinguishable from asking nobody, and it would satisfy the repro "
        f"below without the exchange ever consulting trust"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-303 (a): the sim computes snapshot['delistings'] and drops it — nothing appends the "
        "blacklisted/blacklist_expired events into the chain the run seals, so the delisting "
        "decision is recorded nowhere an exchange, an auditor or an appeal can read it; remove "
        "this marker with the fix"
    ),
)
def test_t303_a_delisting_the_run_computes_lands_in_the_chain_the_run_seals() -> None:
    """The decision is computed. S2 is only closed when it is also RECORDED.

    Behavioural on purpose. The nine T-237 gates above grade ``snapshot['delistings']``'s
    payload, idempotence and threshold — all of which pass — and an AST sweep for "some module
    both reads ``delistings`` and calls ``append``" would be closed by one added subscript in
    ``services/sim/src/runner.py``, which already contains a bare ``append(...)`` 44 lines
    earlier inside a loop that has finished by then. Co-occurrence in a file is not data flow.

    So this runs the chain instead of scanning for it: drive the only product caller of
    ``build_snapshot``, then ask the sealed event stream whether the delisting it computed is
    in there. The negative half is asserted too — a store the run did NOT delist must not
    acquire a blacklisted event — so "append one for everybody" is not a way through.
    """
    run = _t303_simulation_run()
    delistings = run.snapshot["delistings"]
    assert delistings, "no delisting computed; see the armed guard above"

    sealed = {str(event.get("event_id")) for event in run.events}
    dropped = [
        f"{event['kind']} for {event['payload']['store_id']} ({event['event_id']})"
        for event in delistings
        if str(event.get("event_id")) not in sealed
    ]
    assert dropped == [], (
        f"{len(dropped)} of {len(delistings)} delisting decisions the run computed are absent "
        f"from the {len(run.events)}-event chain it sealed, so they are computed and dropped: "
        f"{dropped}. The exchange, an auditor and an appeal all read the ledger, and none of "
        f"them can see a decision that was only ever a dict on a dataclass — note "
        f"SimulationRun.to_json() does not even carry the snapshot, so replay determinism "
        f"never compares it either"
    )

    delisted = {str(event["payload"]["store_id"]) for event in delistings}
    spurious = sorted(
        {
            str(event.get("store_id"))
            for event in run.events
            if str(event.get("kind")) in _BLACKLIST_EVENT_KINDS
            and str(event.get("store_id")) not in delisted
        }
    )
    assert spurious == [], (
        f"the chain carries a delisting event for {spurious}, which the trust snapshot did not "
        f"delist. Recording a decision nobody made is not the fix for dropping the one that "
        f"was made"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-303 (b): apps/exchange/src/auction/routes.py._eligibility lazily builds an empty "
        "StaticSellerEligibility and no product caller ever passes configure_auctions a "
        "trust-backed source, so the served exchange denies every store 'static-eligibility' "
        "and cannot tell an honest store from a delisted one; remove this marker with the fix"
    ),
)
def test_t303_the_served_exchange_can_tell_an_honest_store_from_a_delisted_one() -> None:
    """S2's end: the exchange stops asking a dishonest store. It cannot ask anyone today.

    Measured against the app the deployment actually serves — ``exchange.main.create_app()``
    with no test-side ``configure_auctions`` call — because the wiring gap is invisible to
    every test that configures the app itself. The roster is the manifest's own five stores,
    one of which the trust engine scores far below ``BLACKLIST_THRESHOLD`` and three of which
    it scores well above; a served exchange that answers identically for all five is not
    consulting trust, whatever it is consulting.

    Deliberately NOT asserted here, and asserted in
    ``test_t303_the_exchange_eligibility_gate_never_fails_open`` instead: that the wiring is
    fail-closed. Splitting them matters — the one-line way to satisfy this test alone is a
    source whose default is ELIGIBLE, and that companion is what makes that route red.
    """
    from fastapi.testclient import TestClient
    from fixtures.manifest import load_manifest
    from exchange.main import create_app

    store_ids = [str(store["store_id"]) for store in load_manifest()["stores"]]
    assert len(store_ids) >= 3, f"the manifest roster is too small to discriminate: {store_ids}"

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/auctions", json=_t303_auction_body(store_ids))
    assert response.status_code == 201, f"POST /auctions -> {response.status_code}: {response.text[:400]}"
    body = response.json()

    denials = {entry["store_id"]: entry["reason"] for entry in body["denied"]}
    unasked = sorted(
        store_id
        for store_id, reason in denials.items()
        if "static-eligibility" in str(reason)
    )
    assert unasked == [], (
        f"the served exchange denied {len(unasked)} of {len(store_ids)} rostered stores with a "
        f"reason naming the deterministic double — {unasked} — so it asked "
        f"StaticSellerEligibility, which was built with no rows and answers 'unavailable' for "
        f"every store alive. solicited={body['solicited']}. configure_auctions(app, "
        f"eligibility=...) has no product caller: the only non-test call in the tree is "
        f"e2e/support/s1/flow.py, and it hands over statuses read out of a JSON fixture. So "
        f"the deployed exchange cannot stop asking a dishonest store, because it is not asking "
        f"anyone"
    )
    assert body["solicited"], (
        f"the served exchange solicited no store at all from a {len(store_ids)}-store roster, "
        f"so S2's chain has no end-to-end path even for the honest stores"
    )
