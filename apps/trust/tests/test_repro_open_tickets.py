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
    CHECKOUT and reported ``resolved`` while the image would have raised. ``-S`` disables
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

    **One nonce-tagged verdict, not tokens in stdout.** The probe used to report through
    three unanchored readers — ``SYSPATH ``/``SHIM `` lines read FIRST-match, and
    ``"RESOLVED" in result.stdout`` — on a channel every shipped module can write at import
    time. All three were forged, measured, by module-scope statements in
    ``apps/trust/src/verification/__init__.py``. It now emits ONE line, tagged with a
    per-run nonce that exists only inside the probe's own source, carrying a JSON report
    that this gate parses and asserts fields of. See the comment above ``nonce`` for what
    each forgery did and what is left reachable.
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
        # `target` is read VERBATIM off the Dockerfile and `symlink_to` will accept an
        # absolute host path without complaint. Unchecked, such a link resolves on THIS
        # host: the gate would import happily out of the live checkout while the real image
        # got a link to a path that does not exist in it. Refuse it here, where the message
        # can name the offending Dockerfile line, rather than let it reach the import and
        # be caught — or not — further down. This repo's Dockerfile has no such link; the
        # check is a sensitivity guard, and it reproduces against one that does.
        landing = pathlib.Path(os.path.normpath(spot.parent.resolve() / target))
        assert app.resolve() == landing or app.resolve() in landing.parents, (
            f"apps/trust/Dockerfile links {link} -> {target}, which lands at {landing}, "
            f"outside the container-shaped tree at {app.resolve()}. In the real image that "
            f"link dangles; here it would resolve against this host and hand the gate an "
            f"import the image cannot make. An `ln -s` target under /app has to be relative "
            f"and has to stay inside /app."
        )
        if not spot.is_symlink() and not spot.exists():
            spot.symlink_to(target)

    # A per-run token, minted HERE and interpolated into the probe's SOURCE. It is what
    # makes this gate's verdict unforgeable by anything a shipped module can print, and it
    # replaced three separate readers that were forgeable — all three measured, on this
    # gate, against this repo:
    #
    #   * ``"RESOLVED" in result.stdout`` was an UNANCHORED substring test on a channel
    #     shipped code writes. A module-scope ``print("RESOLVED")`` in
    #     ``apps/trust/src/verification/__init__.py`` made it read True with the verifier
    #     genuinely unresolvable. Only the ``returncode == 0`` conjunct held the gate red —
    #     and a module-scope ``sys.exit(0)`` in the same window forged THAT, at which point
    #     all four guards read green over an image that cannot verify a claim at all.
    #   * the ``SHIM ``/``SYSPATH `` readers took the FIRST matching line. With the probe
    #     genuinely importing ``trust.verification`` from OUTSIDE the container-shaped tree
    #     (reproduced by pointing the Dockerfile's ``ln -s`` at an absolute host path, which
    #     the materialiser below recreates verbatim), the gate correctly reported
    #     ``1 failed``; one module-scope ``print("SHIM <a path inside the temp tree>")``
    #     turned that same broken state into ``1 passed``, because the real
    #     ``shim.__file__`` sat one line BELOW the decoy and was never read.
    #
    # The nonce is NOT passed through the environment, where a module-scope ``os.environ``
    # read would find it, and ``python -S -c <code>`` hands the child no copy of its own
    # source: measured, ``sys.argv == ["-c"]``, ``__main__.__file__`` is unset,
    # ``linecache.getlines("<string>")`` is ``[]`` and ``inspect.getsource(__main__)``
    # raises. It is NOT unreachable, though, and the honest statement of the bound matters
    # more than a comfortable one: a module can walk ``sys._getframe()`` to the ``<string>``
    # frame and read the literal straight out of ``f_code.co_consts`` — measured, it comes
    # back as ``'\nDEADBEEFCAFE {}\n'``.
    #
    # A scraped nonce still does not buy a green gate, and that is what the exactly-one rule
    # below is for. A module that prints a forged verdict produces TWO nonce-tagged lines —
    # its own and the probe's — and two is refused. To be believed a forger has to SUPPRESS
    # the probe's own write, which takes ``os._exit(0)``: not a print, and not something any
    # in-band channel survives. A verdict file and an exit status are forgeable by the same
    # statement, so moving the channel would buy nothing. What this gate claims is therefore
    # exact: nothing a shipped module can PRINT can make it read green.
    nonce = os.urandom(16).hex()

    probe = textwrap.dedent(
        f"""
        import json, sys, traceback
        report = {{"syspath": list(sys.path), "shim": None, "resolved": False, "error": None}}
        try:
            import trust.verification as shim
            report["shim"] = shim.__file__
            shim.verify
            report["resolved"] = True
        except BaseException:
            # BaseException, not Exception: a module-scope `sys.exit()` anywhere under this
            # import becomes a REPORTED error here instead of a silent exit 0.
            report["error"] = traceback.format_exc()
        # The leading newline is load-bearing: a shipped module that wrote a partial line
        # with no trailing newline would otherwise glue its text onto the verdict's prefix
        # and cost this gate a FALSE RED.
        sys.stdout.write("\\n{nonce} " + json.dumps(report) + "\\n")
        sys.stdout.flush()
        raise SystemExit(0 if report["resolved"] else 1)
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

    tagged = [
        line[len(nonce) + 1 :]
        for line in result.stdout.splitlines()
        if line.startswith(f"{nonce} ")
    ]
    assert len(tagged) == 1, (
        f"the container-shaped probe emitted {len(tagged)} nonce-tagged verdict lines, not "
        f"one, so this gate has nothing it is entitled to grade. ZERO does not mean 'clean' "
        f"— it means the probe never reached its own final write, which is where a "
        f"module-scope `os._exit` or a hard crash lands; MORE THAN ONE means something "
        f"other than the probe produced a line carrying this run's nonce, and a collision "
        f"is refused here rather than silently resolved by position.\n"
        f"exit={result.returncode}\n"
        f"stdout:\n{result.stdout.strip()[-1200:]}\n"
        f"stderr:\n{result.stderr.strip()[-1200:]}"
    )
    verdict: dict[str, Any] = json.loads(tagged[0])

    def _inside_checkout(entry: str) -> bool:
        if not entry:
            return False
        resolved = pathlib.Path(entry).resolve()
        return resolved == REPO_ROOT or REPO_ROOT in resolved.parents

    # `syspath` is snapshotted by the probe BEFORE it imports anything shipped, so no
    # shipped module runs early enough to influence it — and, unlike the `SYSPATH ` line it
    # replaced, there is no separate line here for a stray print to displace.
    leaked = [entry for entry in verdict["syspath"] if _inside_checkout(entry)]
    assert leaked == [], (
        f"the probe subprocess can see the live checkout on sys.path ({leaked}), so it can "
        f"import the claim verifier from the repo instead of from the container-shaped tree "
        f"and this gate proves nothing about the image. .venv's site-packages/_proxyshop.pth "
        f"puts {REPO_ROOT} and {REPO_ROOT / '.pkgroot'} on sys.path at interpreter startup "
        f"regardless of PYTHONPATH; `-S` is what keeps them off."
    )

    shim_file = verdict["shim"]
    assert shim_file is not None, (
        "the container-shaped tree cannot import `trust.verification` at all, so this gate "
        "is going red BEFORE the verifier lookup it exists to grade — fix the tree, not the "
        f"COPY set:\n{verdict['error']}\n{result.stderr.strip()[-1200:]}"
    )
    assert app.resolve() in pathlib.Path(shim_file).resolve().parents, (
        f"the probe imported trust.verification from {shim_file}, which is outside the "
        f"container-shaped tree at {app.resolve()} — it is grading the live checkout"
    )

    # `returncode == 0` stays, and stays FIRST: it is the conjunct that a stray print cannot
    # forge, and the probe derives it from `resolved` rather than from having merely run.
    assert result.returncode == 0 and verdict["resolved"] is True, (
        "the trust image's COPY set cannot resolve `trust.verification.verify` "
        f"(exit={result.returncode}, resolved={verdict['resolved']!r}):\n"
        f"{verdict['error']}\n{result.stderr.strip()[-1200:]}\n"
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
_T172_COLLECTOR = """\
import json, os, pathlib
import pytest
from proxyshop_support import service_markers

_RECORDS = []


# `tryfirst`, and the docker filter applied here rather than by `-m docker`. Measured: with
# `trylast`, a `services_for` that REFUSES an undeclared item makes conftest raise a
# UsageError that aborts collection before this hook ever runs, so the corpus was written
# EMPTY and the gate died on the return code instead of naming the eight offenders. Running
# first, and doing our own marker filtering, means the corpus is complete whatever conftest
# does afterwards.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        if not any(True for _ in item.iter_markers("docker")):
            continue
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
"""

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

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )

    # TWO passes. The default `python_files` glob is `test_*.py`, so a docker-marked item in a
    # file named otherwise is invisible to a normal collection — and one exists:
    # `proxyshop_support/tests/_service_skip_probe.py::test_probe_whole_stack` carries a bare
    # marker and would sit outside all three layers below. The second pass widens the glob to
    # underscore files; the lint fixtures are deliberately un-importable and are skipped.
    passes: list[tuple[str, list[str]]] = [
        ("default", []),
        (
            "underscore",
            ["-o", "python_files=_*.py", "--ignore-glob=*/lint_fixtures/*"],
        ),
    ]
    merged: dict[str, dict[str, Any]] = {}
    codes: list[int] = []
    tails: list[str] = []
    for name, extra in passes:
        pass_out = tmp_path / f"t172_corpus_{name}.json"
        env["T172_CORPUS_OUT"] = str(pass_out)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-p",
                    "t172_corpus",
                    *extra,
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=240,
            )
        except subprocess.TimeoutExpired as expiry:  # pragma: no cover - a hang is an outcome
            raise AssertionError(
                f"collecting the docker corpus ({name} pass) did not finish in 240s. A gate "
                f"that hangs prints no red at all, so this is a failure and not a slow pass."
            ) from expiry
        codes.append(completed.returncode)
        tails.append(
            f"[{name}] rc={completed.returncode}\n{completed.stdout[-1200:]}\n"
            f"{completed.stderr[-1200:]}"
        )
        if pass_out.is_file():
            for record in json.loads(pass_out.read_text(encoding="utf-8")):
                merged.setdefault(record["nodeid"], record)
    completed_returncode = codes

    records: list[dict[str, Any]] = [merged[nodeid] for nodeid in sorted(merged)]
    # A non-zero return code is TOLERATED when the corpus was still built. Once the fallback
    # is loud, collecting an undeclared item is *supposed* to fail the session — refusing to
    # reason about that run would hand the diagnosis back as "rc=4" and name one offender of
    # eight, instead of letting the assertions below name all of them.
    assert records, (
        f"the collection subprocesses (rc={completed_returncode}) produced no docker corpus "
        f"at all, so every count below would be silently zero.\n" + "\n".join(tails)
    )
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
        # The marker line is SOURCE, so an annotation manufactures its own evidence. Measured:
        # stamping @pytest.mark.docker("postgres", "neo4j-bolt", "redis") on the offenders put
        # the literal "neo4j-bolt" into test_schema_grants.py, its surplus went empty, and this
        # gate went green (2 passed) while the defect ran live -- the same three Postgres-only
        # role-password tests skipped at exit 0 under a Redis-only outage.
        source = re.sub(r"@pytest\.mark\.docker\s*\([^)]*\)", "", source)
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
    outside_default_glob = [
        record
        for record in records
        if not pathlib.Path(record["nodeid"].split("::")[0]).name.startswith("test_")
    ]
    assert outside_default_glob, (
        "the widened collection pass contributed nothing. pytest's default `python_files` "
        "glob is `test_*.py`, so a docker-marked item in a file named otherwise is invisible "
        "to a normal collection; the second pass exists to see those, and at least one lives "
        "in proxyshop_support/tests/_service_skip_probe.py. Contributing zero means the "
        "widening silently stopped working and this gate is back to a partial corpus"
    )
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
      which close the first assertion while leaving every named test just as skippable. The
      second of those had to be EARNED rather than assumed: the marker line is itself source,
      so until it was stripped the annotation supplied the very evidence it needed, and the
      three-service stamp passed all three layers with the defect running live.
    """
    from proxyshop_support import reachability, service_markers

    records = _t172_collect_docker_corpus(tmp_path)
    assert records, "no docker items collected; see the armed-sweep guard above"

    # A refused item counts as undeclared. It used to be excluded (`refusal is None`), which
    # measured as a real hole: making the fallback raise then EMPTIES this list with zero
    # annotations, and the only thing still forcing them was the unrelated `returncode == 0`
    # check above — which holds only because conftest happens to re-raise the ValueError as a
    # UsageError. A conftest that logged-and-widened instead would have left this green with
    # all 8 offenders still bare.
    undeclared = [record for record in records if not _t172_declares_a_service(record)]
    assert undeclared == [], (
        f"{len(undeclared)} of {len(records)} `-m docker` items declare no service — no "
        f"marker argument and no fixture in FIXTURE_SERVICES — so nothing but the fallback "
        f"decides what they need, and a single-store outage they do not depend on skips them "
        f"at exit 0 (or, once the fallback is loud, refuses them at collection). Every one has "
        f"to say what it needs, or drop the marker: "
        f"{[(record['nodeid'], record['refusal']) for record in undeclared]}"
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


def _t259_values_by_key(
    node: Any, found: dict[str, list[Any]] | None = None
) -> dict[str, list[Any]]:
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
    assert {case["event"]["kind"] for case in cases} == {kind.value for kind in LedgerEventKind}, (
        "the generated ledger kinds no longer cover the whole frozen vocabulary"
    )

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
    accepted: list[tuple[dict[str, Any], dict[str, Any], Any, Any]] = []
    for case in cases:
        emitted = trust_event_payload(case)
        runner = AgentRunner(
            {"store_id": case["store_id"]}, sink=object(), submitter=None, mode="shadow"
        )
        try:
            validated = runner.ingest_trust_event(emitted)
        except Exception as refusal:
            rejected.append((case["dim"], type(refusal).__name__, str(refusal).splitlines()[0]))
        else:
            accepted.append((case, emitted, validated, runner.trust_posture))

    assert rejected == [], (
        f"{len(rejected)} of {len(cases)} events trust emits are refused by the store agent's "
        f"real intake (store_agent.modes.AgentRunner.ingest_trust_event, which validates with "
        f"contracts.TrustEventPayload). First refusal: {rejected[0]}. Both suites stay green "
        f"because trust only ever pushes into a local _RecordingSink and the store agent only "
        f"ever ingests its own hand-written dict, so nothing joins the emitted payload to the "
        f"model the intake really parses"
    )

    losses: list[str] = []
    for case, emitted, validated, posture in accepted:
        # Read off the dict TRUST EMITTED, not the object the intake returned. Measured cheat:
        # a fix that deletes the six carriers and has `ingest_trust_event` graft them onto its
        # own return value passes every check below when they are read off `validated` — the
        # gate then cannot tell "trust put the redaction report on the wire" from "the store
        # agent invented it". Because TrustEventPayload forbids extras, a carrier present in
        # `emitted` AND a payload that validates together mean the carrier sits at an address
        # the contract admits, so it really did cross.
        seen = _t259_values_by_key(emitted)
        landed_keys = set(_t259_values_by_key(validated.model_dump(mode="json")))

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
                losses.append(f"{case['store_id']}: {carrier!r} is nowhere on the emitted payload")
            elif carrier not in landed_keys:
                losses.append(
                    f"{case['store_id']}: {carrier!r} was emitted but is absent from the "
                    f"payload the intake accepted"
                )
        # Exactly one occurrence, and it must EQUAL the planted count. Measured cheat: a
        # shotgun of candidates (`[{"redacted_fields": 0}, ... {"redacted_fields": 3}]`) parked
        # in the open event payload satisfies a set-membership test for every case at once,
        # and that membership test was the only load-bearing assertion in this block.
        reported = seen.get("redacted_fields", [])
        if len(reported) != 1 or reported[0] != case["planted_identity_keys"]:
            losses.append(
                f"{case['store_id']}: redacted_fields reads {reported} but exactly "
                f"{case['planted_identity_keys']} identity keys were planted, and exactly one "
                f"report of it may be on the wire"
            )
        disclosed = seen.get("identity_disclosed", [])
        if disclosed != [False]:
            losses.append(
                f"{case['store_id']}: identity_disclosed reads {disclosed}, not [False] — a "
                f"None or a missing flag is not a promise that identity was withheld"
            )
        policy = seen.get("policy", [])
        if len(policy) != 1 or not (isinstance(policy[0], str) and policy[0].strip()):
            losses.append(
                f"{case['store_id']}: policy reads {policy}; the field is the auditable "
                f"statement of WHY the scrub happened and an empty or absent one says nothing"
            )
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
    from sim.runner import run_simulation

    from fixtures.manifest import load_manifest

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
            {"store_id": str(store_id), "tier": 1, "list_price": 19.99} for store_id in store_ids
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
    assert run.chain_ok, (
        "the simulation's own event chain does not verify; nothing below is trustworthy"
    )
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
    scores = {store_id: entry["score"] for store_id, entry in run.snapshot["stores"].items()}
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
        "T-303 (a): the sim computes snapshot['delistings'] and drops it — no blacklisted or "
        "blacklist_expired event is ever handed to trust.events' append seam, so the delisting "
        "decision is sealed nowhere an exchange, an auditor or an appeal can read it; remove "
        "this marker with the fix"
    ),
)
def test_t303_a_delisting_the_run_computes_is_sealed_by_the_ledger_writer(
    monkeypatch: Any,
) -> None:
    """The decision is computed. S2 is only closed when it is also SEALED.

    Behavioural, and measured at the WRITER rather than on the run's report. Two earlier
    shapes of this gate were both wrong, and an adversarial lane proved both:

    * An AST sweep for "some module both reads ``delistings`` and calls ``append``" is closed
      by one added subscript in ``services/sim/src/runner.py``, which already holds a bare
      ``append(...)`` 44 lines earlier inside a loop that has finished by then. Co-occurrence
      in a file is not data flow.
    * Comparing ``event_id`` against ``run.events`` is worse than useless:
      ``sim.runner.normalise_event`` deliberately DROPS ``event_id`` and ``ts`` (a UUID and a
      wall clock, so two runs of one seed agree), which makes that set the single string
      ``"None"``. Measured consequences — the honest fix (append each delisting through
      ``trust.events.append``, then verify) FAILED that gate, while splicing the delistings
      into the reported list after ``verify()`` PASSED it. It graded the exact inverse of the
      property. ``run.chain_ok`` cannot rescue it either: ``verify()`` runs before
      ``build_snapshot`` produces the delistings, and the flag is a stored field.

    So this spies on ``InMemoryEventStore.append`` — the one seam ``trust.events.append``
    delegates to, whichever route a caller takes — and asks whether a delisting ever went
    through it. A decision that never reaches the writer is not in the ledger, however it is
    reported.

    And it binds the answer to ONE STORE. Spying on the class alone would accept a fix that
    appends the delistings to a throwaway ``InMemoryEventStore()`` and leaves the run's real
    chain untouched: the spy would see them, and ``chain_ok`` would still be True because the
    real store verified fine. So the appends are grouped by instance and only the store that
    took the run's own episode events counts. Every spied ``self`` is kept alive for the
    duration, because ``id()`` is only unique among LIVE objects and a discarded throwaway
    could otherwise inherit the principal store's id.
    """
    import collections

    from trust.events.store import InMemoryEventStore

    appends: list[tuple[int, dict[str, Any]]] = []
    alive: list[Any] = []
    original = InMemoryEventStore.append

    def spy(self: Any, event: Any) -> Any:
        alive.append(self)
        appends.append((id(self), dict(event)))
        return original(self, event)

    monkeypatch.setattr(InMemoryEventStore, "append", spy)
    run = _t303_simulation_run()

    per_store = collections.Counter(store for store, _ in appends)
    assert per_store, (
        "the append spy saw no event at all go through InMemoryEventStore.append during a "
        "whole simulation run. The seam moved, so this gate is watching a door nobody uses "
        "any more and every assertion below would pass by seeing nothing"
    )
    principal, principal_count = per_store.most_common(1)[0]
    assert principal_count >= 20, (
        f"the busiest event store took only {principal_count} appends during a whole "
        f"simulation run (36 when this was written), across {len(per_store)} store(s). The "
        f"run's chain is not being written through this seam any more, so the sweep below "
        f"would conclude from nothing"
    )
    sealed = [event for store, event in appends if store == principal]
    assert run.chain_ok, "the simulation's own chain does not verify; nothing below is trustworthy"

    delistings = run.snapshot["delistings"]
    assert delistings, "no delisting computed; see the armed guard above"

    sealed_ids = {str(event.get("event_id")) for event in sealed}
    elsewhere = {str(event.get("event_id")) for store, event in appends if store != principal}
    dropped = [
        f"{event['kind']} for {event['payload']['store_id']} ({event['event_id']})"
        for event in delistings
        if str(event.get("event_id")) not in sealed_ids
    ]
    assert dropped == [], (
        f"{len(dropped)} of {len(delistings)} delisting decisions the run computed never "
        f"reached the store that seals the run's chain — {len(sealed)} events went into it "
        f"(of {len(appends)} appends across {len(per_store)} store(s)) and none of them was "
        f"one of these: {dropped}. They are computed and dropped."
        + (
            " NOTE: they DID reach some other event store — appending them to a store whose "
            "chain the run does not seal is not sealing them, which is the throwaway-store "
            "shape this instance grouping exists to refuse."
            if any(str(event.get("event_id")) in elsewhere for event in delistings)
            else ""
        )
        + " The "
        "exchange, an auditor and an appeal all read the ledger, and none of them can see a "
        "decision that was only ever a dict on a dataclass; SimulationRun.to_json() does not "
        "even carry the snapshot, so replay determinism never compares it either"
    )

    delisted = {str(event["payload"]["store_id"]) for event in delistings}
    spurious = sorted(
        {
            str(event.get("store_id"))
            for event in sealed
            if str(event.get("kind")) in _BLACKLIST_EVENT_KINDS
            and str(event.get("store_id")) not in delisted
        }
    )
    assert spurious == [], (
        f"the writer sealed a delisting event for {spurious}, which the trust snapshot did not "
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

    STRENGTHENED. A control lane executed the cheat this originally admitted: a hardcoded
    ``StaticSellerEligibility({<the five manifest ids>: "eligible"})`` in ``create_app()``,
    importing nothing from trust and consulting no score, turned it green — because
    "nobody is denied with a static-eligibility reason" is satisfied VACUOUSLY by
    ``denied == []``, which made the gate EASIER the emptier the denial list, inverting the
    property. Under that cheat ``store-brightbean`` — scored 0.073, five times below the
    threshold, and explicitly delisted by the real verdict — was solicited.

    So the ground truth is now the run's OWN verdict rather than the roster's size: the store
    the trust engine delisted must not be solicited, and a store it scored above the threshold
    must be. Both come from ``_t303_simulation_run()``, so nothing here is a number typed into
    the gate, and a hardcoded table cannot satisfy both halves without transcribing a verdict
    it never computed.
    """
    from exchange.main import create_app
    from fastapi.testclient import TestClient
    from trust.scoring import BLACKLIST_THRESHOLD

    from fixtures.manifest import load_manifest

    store_ids = [str(store["store_id"]) for store in load_manifest()["stores"]]
    assert len(store_ids) >= 3, f"the manifest roster is too small to discriminate: {store_ids}"

    run = _t303_simulation_run()
    delisted = {str(event["payload"]["store_id"]) for event in run.snapshot["delistings"]}
    healthy = {
        store_id
        for store_id, entry in run.snapshot["stores"].items()
        if float(entry["score"]) >= BLACKLIST_THRESHOLD and str(store_id) not in delisted
    }
    assert delisted and healthy, (
        f"the run produced no store on one side of the threshold — delisted={sorted(delisted)}, "
        f"healthy={sorted(healthy)} — so the two assertions below cannot discriminate"
    )

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/auctions", json=_t303_auction_body(store_ids))
    assert response.status_code == 201, (
        f"POST /auctions -> {response.status_code}: {response.text[:400]}"
    )
    body = response.json()

    denials = {entry["store_id"]: entry["reason"] for entry in body["denied"]}
    unasked = sorted(
        store_id for store_id, reason in denials.items() if "static-eligibility" in str(reason)
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
    solicited = {
        str(entry) if isinstance(entry, str) else str(entry.get("store_id"))
        for entry in body["solicited"]
    }
    assert solicited & healthy, (
        f"the served exchange solicited none of the stores the trust engine scores at or above "
        f"BLACKLIST_THRESHOLD ({sorted(healthy)}); it solicited {sorted(solicited)} out of a "
        f"{len(store_ids)}-store roster. S2's chain has no end-to-end path even for the honest "
        f"stores"
    )
    still_asked = sorted(solicited & delisted)
    assert still_asked == [], (
        f"the served exchange is still asking {still_asked}, which the trust engine delisted in "
        f"this very run (scores: "
        f"{ {k: round(float(v['score']), 4) for k, v in run.snapshot['stores'].items()} }, "
        f"threshold {BLACKLIST_THRESHOLD}). S2 says the exchange stops asking a dishonest "
        f"store; a source that answers 'eligible' for a store trust has blacklisted is not "
        f"consulting trust, whatever else it is doing"
    )


# ======================================================================================
# T-256 — T-065's persistence half: five tables, no writer, and no caller either
# ======================================================================================
#: The five ledger tables T-065's objective says a verification result lands in. Names only —
#: every column, constraint and vocabulary below is parsed out of the migration itself, so
#: this gate cannot drift away from the schema it is grading.
_T256_TABLES = (
    "ledger.claims",
    "ledger.claim_verifications",
    "ledger.verification_evidence_refs",
    "ledger.trust_observations",
    "ledger.trust_scores",
)

#: The migration that owns those tables. Postgres-only by construction (``jsonb``,
#: ``timestamptz``, ``gen_random_uuid()``, ``plpgsql``), which is exactly why this gate reads
#: the DDL and drives a recording double instead of opening a database: this file's own
#: contract is that nothing in it may skip, and a datastore-backed test skips whenever the
#: compose stack is down — and a skip is not an xfail, so ``strict=True`` would never fire.
_T256_MIGRATION = "db/migrations/0002_ledger_tables.sql"

#: A trust-side callable that persists a verification outcome, found by capability rather than
#: by a pinned name so the fix is free to choose its own spelling.
_T256_SEAM_ACTION = re.compile(r"persist|record|write|save|insert|append", re.IGNORECASE)
_T256_SEAM_SUBJECT = re.compile(r"verif|claim|observation|trust_score", re.IGNORECASE)

#: Where a caller has to live for the seam to be reachable in something that ships. e2e
#: support and tests do not count: ``observation_from_claim`` was added to answer this very
#: class of finding and its only caller in the tree is ``e2e/support/s1/flow.py``, so a
#: function whose sole caller is a harness is the precedent this excludes, not a hypothetical.
_T256_SHIPPED_CALLER = re.compile(r"^(apps/[^/]+(/svc)?|services/[^/]+)/src/")


def _t256_migration_sql() -> str:
    path = REPO_ROOT / _T256_MIGRATION
    assert path.is_file(), (
        f"{_T256_MIGRATION} is missing; the five tables' DDL is ground truth here"
    )
    return path.read_text(encoding="utf-8")


def _t256_table_ddl(sql: str, table: str) -> str:
    """The body of one ``create table`` statement, parenthesis-balanced.

    Balanced rather than a lazy regex to the next ``);`` because these tables carry
    ``check (...)`` constraints with nested parentheses, and a lazy match silently truncates
    the column list — which would make every "is this column required" answer below quietly
    wrong in the permissive direction.
    """
    match = re.search(rf"create\s+table[^(]*?\b{re.escape(table)}\b\s*\(", sql, re.IGNORECASE)
    assert match is not None, f"{_T256_MIGRATION} no longer declares {table}"
    depth, start = 0, match.end() - 1
    for index in range(start, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return sql[start + 1 : index]
    raise AssertionError(f"unbalanced parentheses in the {table} definition")


def _t256_check_values(body: str, column: str) -> list[str]:
    """The literal vocabulary a ``check (<column> in (...))`` constraint pins, in order."""
    match = re.search(rf"{re.escape(column)}\s+in\s*\(([^)]*)\)", body, re.IGNORECASE | re.DOTALL)
    return re.findall(r"'([^']+)'", match.group(1)) if match else []


def _t256_required_columns(body: str) -> list[str]:
    """Columns the database refuses an insert without: NOT NULL and carrying no DEFAULT.

    These are exactly what a mock cursor accepts and Postgres rejects on the first row, which
    is how a persistence "fix" can pass every generated case against SQL that has never run.
    """
    required: list[str] = []
    reserved = {"constraint", "check", "unique", "primary", "foreign", "create", "table"}
    for line in body.splitlines():
        stripped = line.strip().rstrip(",")
        column = re.match(r"^([a-z_][a-z0-9_]*)\s+\S", stripped, re.IGNORECASE)
        if column is None or column.group(1).lower() in reserved:
            continue
        if re.search(r"\bnot\s+null\b", stripped, re.IGNORECASE) and not re.search(
            r"\bdefault\b", stripped, re.IGNORECASE
        ):
            required.append(column.group(1))
    return required


def _t256_constraint_protected(body: str) -> bool:
    """Whether a replay can be refused by the DATABASE rather than by the writer.

    True only when the table carries a multi-column UNIQUE or a composite PRIMARY KEY over
    real columns. ``ledger.trust_observations`` has neither — its only unique index is the
    server-generated ``observation_id`` default — so ``on conflict do nothing`` there is
    vacuous: measured on live Postgres, three identical calls still wrote three rows with the
    clause in place. A gate that accepts the clause as proof grades a substring.
    """
    return bool(
        re.search(r"\bunique\s*\(", body, re.IGNORECASE)
        or re.search(r"\bprimary\s+key\s*\(", body, re.IGNORECASE)
    )


def _t256_inserts(connection: Any, table: str) -> list[str]:
    """Every statement this recorder saw that inserts into ``table``."""
    return [
        statement
        for statement, _ in connection.log
        if re.search(rf"insert\s+into\s+{re.escape(table)}\b", statement, re.IGNORECASE)
    ]


def _t256_cases() -> list[dict[str, Any]]:
    """The FULL claim_type x status cross product, each with randomized surroundings.

    Full rather than sampled because both vocabularies are small and frozen in the migration's
    own CHECK constraints: a stub that hard-codes one dimension or one status passes 1 case of
    56 rather than getting lucky. The surroundings are drawn — never pinned — so every
    assertion below is a function of the generated input rather than of a constant.
    """
    import random
    import uuid

    from claim_verification import VERIFICATION_STATUSES
    from trust.scoring import CLAIM_TYPE_DIMENSIONS

    rng = random.Random(0x256 ^ random.SystemRandom().getrandbits(48))
    cases: list[dict[str, Any]] = []
    for claim_type in sorted(CLAIM_TYPE_DIMENSIONS):
        for status in VERIFICATION_STATUSES:
            cases.append(
                {
                    "store_id": f"store-{rng.randrange(10**9)}",
                    "claim_ref": f"claim-{claim_type}-{rng.randrange(10**9)}",
                    "claim_type": claim_type,
                    "key": f"{claim_type}.value",
                    "status": status,
                    "confidence": round(rng.uniform(0.0, 1.0), 6),
                    # A real UUID, because the column is `uuid not null`. Measured: with
                    # `f"snapshot-{n}"` here, live Postgres answers
                    # `InvalidTextRepresentation: invalid input syntax for type uuid`, so
                    # every one of the 56 cases was un-insertable and the conformance
                    # assertion below could only ever have been the column-name check it
                    # already is. A gate defanged by its own fixture data.
                    "catalog_snapshot_id": str(uuid.UUID(int=rng.getrandbits(128))),
                    "verifier_version": f"verifier-{rng.randrange(100)}.0.0",
                    "evidence_refs": [
                        f"evidence-{rng.randrange(10**9)}" for _ in range(rng.randint(1, 3))
                    ],
                    "observed_at": AS_OF,
                }
            )
    return cases


class _T256RecordingCursor:
    """A DB-API cursor that remembers every statement instead of executing one."""

    def __init__(self, log: list[tuple[str, Any]], seen: dict[tuple[str, str], int]) -> None:
        self._log = log
        self._seen = seen
        self._last = ""
        self._params: Any = None
        self.description = None
        self.rowcount = -1

    def execute(self, sql: Any, params: Any = None) -> _T256RecordingCursor:
        self._log.append((str(sql), params))
        self._last = str(sql)
        self._params = params
        return self

    def executemany(self, sql: Any, seq: Any = None) -> _T256RecordingCursor:
        for params in seq or ():
            self._log.append((str(sql), params))
        return self

    #: What a ``returning claim_id`` / ``returning verification_id`` reads back. A cursor that
    #: answers ``None`` makes the writer pass NULL for the very foreign keys the migration
    #: marks NOT NULL, so a correct writer would look broken and an incorrect one identical.
    RETURNED_ID = "00000000-0000-0000-0000-0000000000ff"

    def _answer(self) -> Any:
        """What the last statement would plausibly have read back.

        Contextual and, for one route, STATEFUL — because two rounds of measurement showed a
        double that cannot answer differently on a replay grades the correct writer exactly
        like the broken one.

        * ``insert ... on conflict ... do nothing ... returning <x>_id`` returns a row the
          first time a given (statement, params) pair is seen and **nothing** afterwards. That
          is what real Postgres does, and it is the channel by which a writer learns "this was
          a replay" — the correct design gates its trust_observations insert on exactly that.
          ``do update ... returning`` is deliberately NOT suppressed: it legitimately returns a
          row on every call, which is what the ``claims`` upsert relies on.
        * an aggregate needs a NUMBER; answering it a uuid made ``int()`` raise on every case
          and errored the whole sweep before a single assertion ran.

        What it still cannot model, stated rather than hidden: durability. An implementation
        that remembers replays in a process-local ``set()`` looks identical to one that asks
        the database, so the replay below re-imports the seam between the two calls, which
        clears process memory and leaves this connection's state intact.
        """
        statement = " ".join(self._last.split()).lower()
        key = (statement, repr(self._params))
        occurrence = self._seen.get(key, 0)
        self._seen[key] = occurrence + 1

        if "insert" in statement and re.search(r"on\s+conflict", statement):
            suppressible = not re.search(r"do\s+update", statement)
            if suppressible and re.search(r"\breturning\b", statement):
                return (self.RETURNED_ID,) if occurrence == 0 else None
        if re.search(r"returning\s+[\w.\"]*id\b", statement):
            return (self.RETURNED_ID,)
        if re.search(r"\b(count|max|min|sum|coalesce)\s*\(", statement):
            return (0,)
        return (self.RETURNED_ID,)

    def fetchone(self) -> Any:
        return self._answer()

    def fetchall(self) -> list[Any]:
        return [self._answer()]

    def close(self) -> None:
        return None

    def __enter__(self) -> _T256RecordingCursor:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False


class _T256RecordingConnection:
    """The smallest thing a psycopg-shaped writer can be handed without a database."""

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        #: Shared across every cursor this connection hands out, because a real database's
        #: state does not reset when a writer opens a new cursor.
        self.seen: dict[tuple[str, str], int] = {}

    def cursor(self, *_: Any, **__: Any) -> _T256RecordingCursor:
        return _T256RecordingCursor(self.log, self.seen)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def execute(self, sql: Any, params: Any = None) -> _T256RecordingCursor:
        return _T256RecordingCursor(self.log, self.seen).execute(sql, params)

    def __enter__(self) -> _T256RecordingConnection:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False


def _t256_find_seams() -> list[tuple[str, str, Any]]:
    """Every trust-side callable that looks like it persists a verification outcome.

    Found by capability across the trust package's public surface — ``(module, name, obj)`` —
    so the fix picks its own spelling and its own home, and this gate grades whether the
    behaviour exists rather than whether one particular identifier does.
    """
    import importlib

    found: list[tuple[str, str, Any]] = []
    for module_name in (
        "trust.verification",
        "trust.ledger",
        "trust.events",
        "trust.scoring",
        "trust.snapshot",
        "trust.reconcile",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # a package that will not import cannot be the seam
            continue
        for name in getattr(module, "__all__", ()) or dir(module):
            if name.startswith("_"):
                continue
            if not (_T256_SEAM_ACTION.search(name) and _T256_SEAM_SUBJECT.search(name)):
                continue
            attribute = getattr(module, name, None)
            if callable(attribute) and not isinstance(attribute, type):
                found.append((module_name, name, attribute))
    return found


def _t256_seam_home(seam: Any) -> pathlib.Path | None:
    """The directory a seam is really defined in, resolved through symlinks.

    Not derived from the module NAME. ``trust.verification`` is reached through the tracked
    ``.pkgroot/trust -> ../apps/trust/src`` symlink, so the string ``trust/verification`` never
    appears in any real product path and a name-derived exclusion filters nothing at all —
    measured: 0 of 272 swept files, for all six searched modules. That made the caller check
    satisfiable by the seam's own ``def`` line, which is the whole demand inverted.
    """
    try:
        source = inspect.getsourcefile(seam)
    except TypeError:
        return None
    return pathlib.Path(source).resolve().parent if source else None


def _t256_inside(path: pathlib.Path, home: pathlib.Path | None) -> bool:
    """Whether ``path`` lives in the seam's own package, resolved on both sides."""
    if home is None:
        return False
    return path.resolve().is_relative_to(home)


def _t256_calls(source: str, name: str) -> bool:
    """Whether this module CALLS ``name`` — an AST call site, not a text match.

    A regex for ``name(`` matches the ``def`` line, a docstring, a comment and a string
    literal equally well, so it cannot tell a caller from a definition. Only an
    :class:`ast.Call` whose callee resolves to that identifier counts.
    """
    import warnings

    try:
        with warnings.catch_warnings():
            # Product sources carry regexes written as plain strings; parsing them here
            # re-emits their SyntaxWarnings against `<unknown>`, which is noise this gate adds.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id == name:
            return True
        if isinstance(callee, ast.Attribute) and callee.attr == name:
            return True
    return False


def test_t256_the_verification_persistence_sweep_is_armed() -> None:
    """The five tables, the two vocabularies and the cross product all still exist.

    Not xfail. The repro below iterates the full ``claim_type x status`` cross product, and
    the cheapest way to silence it is for one of those vocabularies to shrink — a sweep that
    quietly drops to one case still reports the same "passed". This is also where the DDL
    reader is proved to have read something: a regex that silently matched nothing would make
    every "is this column required" answer below vacuously true, which is this repo's own
    defanged-by-its-own-fixture failure.
    """
    from claim_verification import VERIFICATION_STATUSES
    from contracts.ledger import LEDGER_EVENT_KINDS, LEDGER_PAYLOAD_SHAPES
    from trust.scoring import CLAIM_TYPE_DIMENSIONS

    sql = _t256_migration_sql()
    bodies = {table: _t256_table_ddl(sql, table) for table in _T256_TABLES}

    claim_types = _t256_check_values(bodies["ledger.claims"], "claim_type")
    assert len(claim_types) >= 10, (
        f"parsed only {len(claim_types)} claim types out of {_T256_MIGRATION}'s "
        f"claims_claim_type_check (14 when this was written): {claim_types}. The cross product "
        f"below is built from this list, so a reader that stopped matching would shrink the "
        f"sweep to nothing and still report a pass"
    )
    assert set(claim_types) == set(CLAIM_TYPE_DIMENSIONS), (
        f"the migration's claim-type vocabulary and trust.scoring.CLAIM_TYPE_DIMENSIONS have "
        f"drifted: only in the migration "
        f"{sorted(set(claim_types) - set(CLAIM_TYPE_DIMENSIONS))}, only in the mapping "
        f"{sorted(set(CLAIM_TYPE_DIMENSIONS) - set(claim_types))}"
    )

    statuses = _t256_check_values(bodies["ledger.claim_verifications"], "status")
    assert set(statuses) == set(VERIFICATION_STATUSES), (
        f"the migration pins {sorted(statuses)} as verification statuses and the verifier "
        f"publishes {sorted(VERIFICATION_STATUSES)}"
    )

    for table, body in bodies.items():
        assert _t256_required_columns(body), (
            f"{table} parsed to zero NOT-NULL-without-DEFAULT columns, so the SQL conformance "
            f"assertion in the repro would accept an INSERT naming nothing at all"
        )

    assert "claim_verified" in LEDGER_EVENT_KINDS, (
        "claim_verified is no longer a frozen ledger kind, so T-065's 'emit claim_verified' "
        "half has nothing left to be measured against"
    )
    assert LEDGER_PAYLOAD_SHAPES["claim_verified"], (
        "contracts pins no payload shape for claim_verified any more"
    )

    cases = _t256_cases()
    assert len(cases) == len(claim_types) * len(statuses), (
        f"the generator produced {len(cases)} cases, not the full "
        f"{len(claim_types)}x{len(statuses)} cross product"
    )
    assert {case["claim_type"] for case in cases} == set(claim_types), (
        "the generated cases no longer exercise every claim type"
    )
    assert {case["status"] for case in cases} == set(statuses), (
        "the generated cases no longer exercise every verification status"
    )
    assert len({case["store_id"] for case in cases}) == len(cases), (
        "generated store ids collide, so the sweep is narrower than it counts"
    )

    import uuid

    uuid_columns = {
        column
        for body in bodies.values()
        for column in re.findall(
            r"^\s*([a-z_][a-z0-9_]*)\s+uuid\b", body, re.MULTILINE | re.IGNORECASE
        )
    }
    assert uuid_columns, f"{_T256_MIGRATION} declares no uuid column; the reader lost its shape"
    for column in sorted(uuid_columns & set(cases[0])):
        for case in cases:
            try:
                uuid.UUID(str(case[column]))
            except ValueError:
                pytest.fail(
                    f"the generated {column!r} is {case[column]!r}, which is not a UUID — and "
                    f"{_T256_MIGRATION} declares that column `uuid`. Live Postgres answers "
                    f"`invalid input syntax for type uuid`, so every generated case would be "
                    f"un-insertable and the conformance assertion in the repro could only ever "
                    f"be the column-name check it already is: a gate defanged by its own "
                    f"fixture data"
                )

    assert _t256_calls("persist_it(1)", "persist_it"), "the call detector sees no plain call"
    assert _t256_calls("mod.persist_it(1)", "persist_it"), "the call detector sees no method call"
    assert not _t256_calls("def persist_it(a):\n    pass\n", "persist_it"), (
        "the call detector counts a DEFINITION as a call, so a seam nobody invokes would read "
        "as called — measured, that is exactly how this gate's caller demand was inverted"
    )
    assert not _t256_calls('x = "persist_it(1)"\n# persist_it(2)\n', "persist_it"), (
        "the call detector counts a string literal or a comment as a call"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-256: T-065's persistence half is unimplemented — ledger.claims, "
        "claim_verifications, verification_evidence_refs, trust_observations and trust_scores "
        "all exist in db/migrations/0002 and the only INSERT in the product tree targets "
        "ledger.commerce_events, so no trust-side seam persists a verification outcome and "
        "nothing calls one; remove this marker with the fix"
    ),
)
def test_t256_a_verification_result_is_persisted_to_the_tables_it_was_specified_for() -> None:
    """A verification that writes nothing is graded, correctly, as writing nothing.

    T-065's acceptance item 2 — "re-running the same (claim, snapshot, verifier version)
    writes nothing" — is satisfied trivially by a pure function, which is how the persistence
    half stayed unimplemented behind a green ticket. The schema disagrees about what the item
    meant: the migration reserves ``claim_verifications_idempotency_key UNIQUE (claim_id,
    catalog_snapshot_id, verifier_version)`` with a comment naming that very acceptance item,
    so it was always about ROWS.

    Four demands, in an order where each blocks the cheapest way past the one before it:

    1. **a seam exists** — some trust-side callable persists a verification outcome;
    2. **something that ships calls it** — an uncalled function that formats INSERT strings
       leaves the ticket's own claim ("nothing writes any of them") true after the fix, and the
       precedent for exactly that sits in the scoped file: ``observation_from_claim``'s only
       caller anywhere in the tree is ``e2e/support/s1/flow.py``;
    3. **its SQL is executable** — every column the migration marks NOT NULL with no DEFAULT
       must be named. A recording cursor accepts an INSERT that omits all of them, so without
       this the whole cross product passes against SQL Postgres would reject on row one;
    4. **a replay writes no second ROW** — compared against a ONE-call baseline, with
       ``on conflict`` accepted as a defence only where a UNIQUE exists for it to catch.

    WHAT THIS DOUBLE CAN AND CANNOT SEE, because the next person to touch demand 4 will hit
    this and three rounds of live-Postgres measurement went into learning it.

    It can see the ``on conflict ... do nothing ... returning`` route: the recording cursor
    answers a row the first time a ``(statement, params)`` pair arrives and nothing after,
    which is what Postgres does and is the channel the correct writer reads. It deliberately
    does NOT model ``select``-then-skip or a ``count(*)`` gate, because doing so means
    reimplementing a query engine inside a test double; a writer that reads the database in
    one of those shapes will fail here even though it is correct, and the honest response is
    this paragraph rather than a wider double. It also cannot see DURABILITY at all — an
    in-process ``set()`` is indistinguishable from a table — which is why the replay call
    crosses an ``importlib.reload``.

    THE REAL REMEDY IS A MIGRATION, and it is a defect in the product rather than in this
    gate. ``ledger.trust_observations`` carries no uniqueness over its real columns: its only
    unique index is the primary key on ``observation_id``, declared
    ``uuid ... default gen_random_uuid()`` and therefore fresh on every INSERT, so there is no
    arbiter a bare ``ON CONFLICT DO NOTHING`` could ever match. Measured on a dedicated
    database: three identical calls leave the table at 3 rows both without the clause and
    with it, the rows sharing ``verification_id``, ``store_id``, ``dim`` and ``weight`` and
    differing only in the generated id, while ``claim_verifications`` stays at 1 because it
    has ``claim_verifications_idempotency_key UNIQUE (claim_id, catalog_snapshot_id,
    verifier_version)`` to arbitrate. A redelivered queue message or a replay is enough to
    double-count an observation in a store's trust score, silently. Until a
    ``UNIQUE (verification_id, dim, observation_type)`` exists, correctness here rests
    entirely on writer discipline — which is exactly why this demand has to infer intent from
    SQL text instead of checking something the database enforces.
    """
    import importlib

    seams = _t256_find_seams()
    assert seams, (
        "no trust-side callable persists a verification outcome. Searched the public surface "
        "of trust.verification / trust.ledger / trust.events / trust.scoring / trust.snapshot "
        "/ trust.reconcile for a name that is both an action "
        "(persist|record|write|save|insert|append) and a subject "
        "(verif|claim|observation|trust_score), and found none — which agrees with the tree: "
        "the only INSERT into ledger.* anywhere in product code targets "
        f"ledger.commerce_events. The five tables {list(_T256_TABLES)} have no writer at all, "
        "so T-065's acceptance item 2 grades pure-function idempotency and nothing else"
    )

    shipped = {
        path: source
        for path in _product_python_files()
        if _T256_SHIPPED_CALLER.match(str(path.relative_to(REPO_ROOT)))
        and (source := path.read_text(encoding="utf-8"))
    }
    assert shipped, "the product file sweep found no shipped module to search for callers"
    uncalled: list[str] = []
    for module_name, name, seam_object in seams:
        callers = [
            str(path.relative_to(REPO_ROOT))
            for path, source in shipped.items()
            if not _t256_inside(path, _t256_seam_home(seam_object)) and _t256_calls(source, name)
        ]
        if not callers:
            uncalled.append(f"{module_name}.{name}")
    assert len(uncalled) < len(seams), (
        f"every persistence seam found is uncalled by anything that ships: {uncalled}. A "
        f"function nobody invokes writes nothing, so the ticket's claim survives its own fix. "
        f"A caller has to live under apps/<app>/src/** or services/<service>/src/** — not in "
        f"tests, and not in e2e support"
    )

    sql = _t256_migration_sql()
    required = {
        table: _t256_required_columns(_t256_table_ddl(sql, table)) for table in _T256_TABLES
    }
    cases = _t256_cases()
    module_name, name, seam = next(
        (entry for entry in seams if f"{entry[0]}.{entry[1]}" not in uncalled), seams[0]
    )
    parameters = set(inspect.signature(seam).parameters)

    connection = _T256RecordingConnection()
    for case in cases:
        arguments = dict(case, connection=connection)
        seam(**{key: value for key, value in arguments.items() if key in parameters})

    written = {
        table: [
            statement
            for statement, _ in connection.log
            if re.search(rf"insert\s+into\s+{re.escape(table)}\b", statement, re.IGNORECASE)
        ]
        for table in _T256_TABLES
    }
    missing = sorted(table for table, statements in written.items() if not statements)
    assert missing == [], (
        f"{module_name}.{name} wrote nothing to {missing} across all {len(cases)} generated "
        f"claim_type x status cases"
    )

    incomplete: set[str] = set()
    for table, statements in written.items():
        for statement in statements:
            columns = re.search(
                rf"insert\s+into\s+{re.escape(table)}\s*\(([^)]*)\)", statement, re.IGNORECASE
            )
            named = (
                {token.strip().strip('"') for token in columns.group(1).split(",")}
                if columns
                else set()
            )
            absent = sorted(set(required[table]) - named)
            if absent:
                incomplete.add(f"{table} INSERT omits {absent}")
    assert not incomplete, (
        f"{len(incomplete)} generated INSERT shapes omit a column the migration marks NOT NULL "
        f"with no DEFAULT, so Postgres would reject them on the first row while a recording "
        f"cursor takes all {len(cases)}:\n  " + "\n  ".join(sorted(incomplete)[:8])
    )

    once = _T256RecordingConnection()
    twice = _T256RecordingConnection()
    # A FRESH case, never one the 56-case sweep above already wrote. Measured: reusing
    # `cases[0]` let a writer with a process-local memo emit NOTHING at replay time — the
    # sweep had already recorded that key — so the comparison read "0 INSERTs against 0" and
    # called it idempotent. The demand was satisfiable by a writer that had stopped writing.
    replay_case = _t256_cases()[0]
    assert replay_case["claim_ref"] not in {case["claim_ref"] for case in cases}, (
        "the replay case collides with one the sweep already wrote, so a writer that "
        "remembers what it has seen would be graded on a key it has already retired"
    )
    payload = {key: value for key, value in dict(replay_case).items() if key in parameters}
    seam(**dict(payload, connection=once))
    seam(**dict(payload, connection=twice))
    after_first_call = {table: len(_t256_inserts(twice, table)) for table in _T256_TABLES}
    # Re-import the seam before the replay. An idempotency check that lives in a process-local
    # `set()` is not idempotency — it forgets on every restart and never applies across
    # processes — but an in-process double cannot tell it from durable state. Reloading clears
    # module-level memory while leaving this connection's state intact, so only a writer that
    # asks the DATABASE still refuses the second write.
    # Reload the module the seam is DEFINED in, not the package it is exported from.
    # Measured: reloading `trust.verification` re-executes its `__init__` but leaves
    # `trust.verification.persistence` untouched in sys.modules, so a module-level `set()`
    # memo living in the submodule survived and a writer that never asks the database still
    # passed. `seam.__module__` is where the state actually lives.
    replay_seam = seam
    for target in dict.fromkeys((getattr(seam, "__module__", "") or module_name, module_name)):
        try:
            reloaded = importlib.reload(sys.modules.get(target) or importlib.import_module(target))
        except Exception:  # a module that will not reload is graded on the seam we hold
            continue
        replay_seam = getattr(reloaded, name, replay_seam)
    replay_seam(**dict(payload, connection=twice))

    baseline = {table: len(_t256_inserts(once, table)) for table in _T256_TABLES}
    silent = sorted(table for table, count in baseline.items() if count == 0)
    assert silent == [], (
        f"the replay comparison has nothing to compare: a single call wrote no row at all to "
        f"{silent}. 'zero INSERTs and zero INSERTs' is not idempotence, it is a writer that "
        f"has stopped writing — the same defanged-by-its-own-fixture shape as un-insertable "
        f"fixture data, one level up"
        "\n\nWHAT THIS DOUBLE CAN SEE, so you do not debug the wrong thing: it models "
        "`on conflict ... do nothing ... returning`, which answers a row the first time "
        "and nothing on a replay; gating your write on that is the design this grades. "
        "It does NOT model a select-then-skip, a count(*) gate or "
        "`insert ... select ... where not exists` -- those are correct against a real "
        "database and will still fail here. That is a limit of this double, not a "
        "verdict on your code, and it exists because ledger.trust_observations has no "
        "UNIQUE for the database to arbitrate with."
    )

    # A second CONNECTION must see at least what the first one did. Measured: an
    # implementation whose idempotency is a process-local `set()` has its key populated by the
    # baseline call, so the replay connection received NOTHING and "0 INSERTs against 1" read
    # as idempotent. That is not idempotence, it is amnesia about which database it is talking
    # to — the same writer against a fresh database would write nothing at all.
    amnesiac = sorted(
        f"{table}: {after_first_call[table]} INSERT(s) on a second connection's FIRST call, "
        f"{baseline[table]} on the first connection's"
        for table in _T256_TABLES
        if after_first_call[table] != baseline[table]
    )
    assert amnesiac == [], (
        f"the first write to a second connection did not match the first write to the first "
        f"one: {amnesiac}. That is the signature of a writer refusing a connection it has "
        f"never written to -- idempotence held in process memory rather than by the database. "
        f"A memo forgets on every restart and never applies across processes, and against a "
        f"fresh database this writer would produce no rows at all. It is invisible to the "
        f"replay comparison below, because a key the baseline call already retired keeps the "
        f"two-call total no larger than the one-call total"
    )

    doubled: list[str] = []
    for table in _T256_TABLES:
        first = baseline[table]
        replayed = _t256_inserts(twice, table)
        if len(replayed) <= first:
            continue
        extra = replayed[first:]
        protected = _t256_constraint_protected(_t256_table_ddl(sql, table))
        if protected and all(
            re.search(r"on\s+conflict", statement, re.IGNORECASE) for statement in extra
        ):
            continue
        guarded = sum(
            1 for statement in extra if re.search(r"on\s+conflict", statement, re.IGNORECASE)
        )
        doubled.append(
            f"{table}: {first} INSERT(s) on one call, {len(replayed)} on two; "
            + (
                f"{guarded} of the {len(extra)} extra carry ON CONFLICT but this table has no "
                f"UNIQUE for it to catch, so the clause is decoration"
                if not protected
                else f"{guarded} of the {len(extra)} extra carry ON CONFLICT"
            )
        )
    assert doubled == [], (
        f"replaying the identical (claim_ref, catalog_snapshot_id, verifier_version) writes "
        f"more rows the second time: {doubled}. T-065's acceptance 2 is about ROWS, not "
        f"statements. Deferring to a UNIQUE constraint with ON CONFLICT counts as a defence "
        f"ONLY where such a constraint exists — ledger.trust_observations has none beyond its "
        f"server-generated observation_id, so the clause there is decoration and measurably "
        f"still wrote three rows for three identical calls. Not emitting the second INSERT at "
        f"all is always a defence"
        "\n\nWHAT THIS DOUBLE CAN SEE, so you do not debug the wrong thing: it models "
        "`on conflict ... do nothing ... returning`, which answers a row the first time "
        "and nothing on a replay; gating your write on that is the design this grades. "
        "It does NOT model a select-then-skip, a count(*) gate or "
        "`insert ... select ... where not exists` -- those are correct against a real "
        "database and will still fail here. That is a limit of this double, not a "
        "verdict on your code, and it exists because ledger.trust_observations has no "
        "UNIQUE for the database to arbitrate with."
    )


# ======================================================================================
# T-257 — T-112's recorded gate collects none of its own graders
# ======================================================================================
#: A ticket id as it is written anywhere a human writes one: ``T-112``, ``T_112``, ``T 112``.
#: The trailing guard stops ``T-1120`` from matching ``T-112``.
def _t257_ticket_pattern(ticket_id: str) -> re.Pattern[str]:
    number = ticket_id.split("-", 1)[1]
    return re.compile(rf"(?<![A-Za-z0-9])T[-_ ]?{re.escape(number)}(?![0-9])")


#: Words that sit between a shell verify string and its pytest arguments.
_T257_RUNNER_WORDS = frozenset({"uv", "run", "python", "python3", "-m", "pytest", "env", "exec"})


def _t257_testpath_roots() -> list[str]:
    """``testpaths`` read out of ``pyproject.toml``, never transcribed.

    The grader search below walks these, so a root added to the project must widen the search
    rather than leaving a module invisible to it.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^testpaths\s*=\s*\[([^\]]*)\]", text, re.MULTILINE)
    assert match is not None, "pyproject.toml declares no testpaths; the grader search has no roots"
    return re.findall(r'"([^"]+)"', match.group(1))


def _t257_pytest_args(verify: str) -> list[str]:
    """The argv a recorded ``verify`` hands pytest, with the shell and runner prefix removed.

    Parsed with ``shlex`` and replayed VERBATIM rather than approximated by a path glob, so an
    ``-k``, an ``--ignore`` or a ``--deselect`` inside the verify cannot hide from this — the
    collection is done by pytest's own collector, which is the thing the gate is about.
    """
    import shlex

    tokens = shlex.split(verify)
    index = 0
    while index < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]):
        index += 1
    while index < len(tokens) and tokens[index] in _T257_RUNNER_WORDS:
        index += 1
    # `-q` twice collapses pytest's collect-only output to "<file>: <count>" and loses the
    # node ids entirely, which would read as "collected nothing" instead of failing.
    return [token for token in tokens[index:] if token not in {"-q", "--quiet"}]


def _t257_collect(args: list[str]) -> tuple[int, list[str]]:
    """The node ids a recorded verify's argv actually collects, from a real pytest run."""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *args,
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired as expiry:  # pragma: no cover - a hang is a third outcome
        raise AssertionError(
            f"collecting {args} did not finish in 240s; a gate that hangs prints no red at all"
        ) from expiry
    node_ids = [line.strip() for line in completed.stdout.splitlines() if "::" in line.strip()]
    return completed.returncode, node_ids


def _t257_declaring_modules(ticket_id: str) -> dict[str, int]:
    """``{path: test count}`` for every test module whose MODULE DOCSTRING names the ticket.

    The module docstring is the author's statement of what the whole module grades — a
    positional, whole-file claim. Deliberately NOT matched are:

    * comments, which sit outside the AST entirely;
    * function and class docstrings, and assertion messages, which are prose INSIDE a test.

    That exclusion is the whole point. The obvious way to green a "does the gate see its
    graders" check is to type the ticket id into a docstring of a test the recorded verify
    already collects — a change that reads as a legitimate cross-reference and that a reviewer
    would wave through. ``apps/trust/tests/test_schema_grants.py`` sits one word away from
    qualifying that way: five of its collected tests already drive
    ``PROXYSHOP_ROLE_PASSWORD`` through a fresh-volume container.

    A docstring rule alone was still not enough, and a control lane measured it: prepending
    ``T-112: `` to ``test_schema_grants.py``'s own module docstring — eight characters — turned
    this gate green with 0 of the 14 real graders collected, and defeated the armed guard too
    (its ``>= 10`` floor then read 54 instead of 14). So a declaration is only honoured from a
    module inside the ticket's OWN declared ``scope``: T-112 scopes
    ``proxyshop_support/**``, which contains its grader module and does not contain
    ``apps/trust/tests/``. The discriminator comes from the ticket, not from a path typed here.

    Measured alternatives that do NOT discriminate, and why they were rejected — an anchor
    co-occurrence check over each test's reachable source finds 5 false graders inside
    ``test_schema_grants.py`` (its module-level ``COMPOSE_FILE`` plus the role-password
    environment it passes to ``_fresh_volume_postgres``), and narrowing that to anchors inside
    an ``assert`` expression finds 0 in BOTH modules.
    """
    pattern = _t257_ticket_pattern(ticket_id)
    scope = _t257_ticket_scope(ticket_id)
    declaring: dict[str, int] = {}
    for root in _t257_testpath_roots():
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("test_*.py")):
            if {".venv", "node_modules", ".swarm-loop"} & set(path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            if not pattern.search(ast.get_docstring(tree) or ""):
                continue
            relative = str(path.relative_to(REPO_ROOT))
            if not _t257_in_scope(relative, scope):
                continue
            # Module level and class bodies only. `ast.walk` descends into nested scopes, so
            # a `def test_helper` defined INSIDE another test — or one under
            # `if TYPE_CHECKING:` — inflated this floor above anything pytest can collect, and
            # an honest full-file verify then failed the completeness check below.
            bodies = [tree.body] + [
                node.body for node in tree.body if isinstance(node, ast.ClassDef)
            ]
            declaring[relative] = sum(
                1
                for body in bodies
                for node in body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name.startswith("test_")
            )
    return declaring


def _t257_ticket_scope(ticket_id: str) -> list[str]:
    """A ticket's own declared ``scope`` globs, read out of tickets.json."""
    tickets = json.loads((REPO_ROOT / "tickets.json").read_text(encoding="utf-8"))["tickets"]
    ticket = next((entry for entry in tickets if entry["id"] == ticket_id), None)
    assert ticket is not None, f"{ticket_id} is not in tickets.json"
    return [str(entry) for entry in ticket.get("scope") or []]


def _t257_in_scope(relative: str, scope: list[str]) -> bool:
    """Whether a repo-relative path falls inside any of a ticket's declared scope globs.

    A scope entry is either a glob (``proxyshop_support/**``) or a bare path
    (``docker-compose.yml``); a directory-shaped entry covers everything beneath it. An empty
    scope admits everything, because a ticket that declares no scope cannot exclude anyone.
    """
    import fnmatch

    if not scope:
        return True
    for entry in scope:
        stem = entry.rstrip("*").rstrip("/")
        if fnmatch.fnmatch(relative, entry) or (stem and relative.startswith(f"{stem}/")):
            return True
    return False


def _t257_declares(source: str, ticket_id: str) -> bool:
    """Whether one module's source declares a ticket, by the module-docstring rule alone."""
    return bool(_t257_ticket_pattern(ticket_id).search(ast.get_docstring(ast.parse(source)) or ""))


def test_t257_the_grader_discovery_is_armed_and_ignores_prose() -> None:
    """The declaration reader must find the real graders and refuse three kinds of prose.

    Not xfail. Every assertion inside a strict xfail is invisible in a normal run, so a
    discovery rule that quietly stopped finding anything — or started counting comments —
    would leave the repro below passing for the wrong reason while looking identical.
    """
    declaring = _t257_declaring_modules("T-112")
    assert declaring, (
        "no test module's docstring declares T-112, so the repro below has no grader corpus to "
        "ask about and would pass by having nothing to compare against"
    )
    assert max(declaring.values()) >= 10, (
        f"the largest T-112 grader module holds only {max(declaring.values())} tests "
        f"({declaring}); 14 when this was written. A corpus that small means the graders were "
        f"deleted rather than collected, which is the other way to silence this"
    )

    ticket_pattern = _t257_ticket_pattern("T-112")
    assert not ticket_pattern.search("T-1120 and T-11 and XT-112x"), (
        "the ticket pattern matches a longer number or an embedded id, so unrelated tickets "
        "would vote themselves into the corpus"
    )
    assert ticket_pattern.search('"""T_112: underscore spelling."""'), "T_112 no longer matches"

    comment_only = (
        "# T-112 lives elsewhere\n'''T-011: something else.'''\ndef test_x():\n    pass\n"
    )
    assert not _t257_declares(comment_only, "T-112"), (
        "a `# T-112` comment counts as a declaration, so one pasted line would green the repro"
    )
    function_docstring_only = (
        '"""T-011: something else."""\n\n\ndef test_x():\n    """T-112 acceptance 1."""\n    pass\n'
    )
    assert not _t257_declares(function_docstring_only, "T-112"), (
        "a FUNCTION docstring counts as a module declaration — the exact one-word evasion this "
        "rule exists to refuse, since prose inside a collected test would then buy a pass"
    )
    assert _t257_declares('"""T-112: the real thing."""\n', "T-112"), (
        "a module docstring naming the ticket is no longer read as a declaration, so the "
        "reader can never find a grader at all"
    )

    tickets = json.loads((REPO_ROOT / "tickets.json").read_text(encoding="utf-8"))["tickets"]
    verify = next(ticket for ticket in tickets if ticket["id"] == "T-112")["verify"]
    args = _t257_pytest_args(verify)
    assert args, f"T-112's verify parsed to no pytest arguments at all: {verify!r}"
    returncode, node_ids = _t257_collect(args)
    assert returncode == 0, f"replaying T-112's verify as a collection exited {returncode}"
    assert len(node_ids) > 0, (
        f"replaying T-112's recorded verify collected zero node ids from {args}. The repro "
        f"below intersects this list with the grader corpus; an empty list makes it fail for "
        f"the wrong reason and invites the gate to be loosened rather than the ticket fixed"
    )


# T-257 FIXED by amendment 25 (freeze-log seq 55). The strict xfail that stood here said
# T-112's recorded verify was `pytest apps/trust/tests/test_schema_grants.py -q`, which
# collects a module whose docstring declares T-011 and none of T-112's own graders, so the
# gate would stay green with every T-112 behaviour deleted. Amendment 25 repointed T-112
# onto proxyshop_support/tests/test_role_password_end_to_end.py -- the very file this gate's
# own remedy text named -- and the marker flipped to XPASS(strict) on the next full run.
# Removing it is that instruction being followed, not the gate being loosened.
def test_t257_the_recorded_gate_for_t112_collects_at_least_one_of_its_own_graders() -> None:
    """A ticket's recorded gate has to be able to see the tests written to grade it.

    Stronger than the known T-110/T-112 non-discrimination: the gate is not merely unable to
    tell those two tickets apart, it cannot see T-112 at all. Its 56 collected tests come from
    a module that declares itself T-011's, and the module that declares itself T-112's — whose
    own docstring says "The lane gate runs ``pytest proxyshop_support -q``, so these tests are
    executed by the same gate", a belief tickets.json flatly contradicts — is never reached.

    Written as the general invariant and computed dynamically, never as
    ``assert "proxyshop_support" in verify``: a literal expected path would be the example
    rather than the property, and would die to a rename while the defect walked free.
    """
    tickets = json.loads((REPO_ROOT / "tickets.json").read_text(encoding="utf-8"))["tickets"]
    verify = next(ticket for ticket in tickets if ticket["id"] == "T-112")["verify"]
    args = _t257_pytest_args(verify)

    # A bare root — `pytest proxyshop_support -q`, the exact command the grader module's own
    # docstring says the lane gate runs and the most thorough available fix — used to fail this
    # assertion because only `<root>/...` was accepted. Refusing the most complete form of the
    # fix is the gate being wrong, not the fix.
    roots = _t257_testpath_roots()
    named_paths = [
        token
        for token in args
        if not token.startswith("-")
        and any(token == root or token.startswith(f"{root}/") for root in roots)
    ]
    assert named_paths, (
        f"T-112's verify names no path under any testpaths root ({args}), so it is either the "
        f"whole suite wearing a ticket's name or it points outside the project. A ticket gate "
        f"has to say which tests grade it"
    )

    returncode, node_ids = _t257_collect(args)
    assert returncode == 0 and node_ids, "the verify did not collect; see the armed guard above"

    declaring = _t257_declaring_modules("T-112")
    collected_files = {node_id.split("::")[0] for node_id in node_ids}
    unreached = sorted(set(declaring) - collected_files)
    assert unreached == [], (
        f"T-112's recorded gate collects {len(node_ids)} tests from {sorted(collected_files)} "
        f"and never reaches {unreached}, which declare themselves its graders. Every declaring "
        f"module has to be collected, not just one: a five-line module with a matching "
        f"docstring and a single `assert True` is otherwise a cheaper way to satisfy this gate "
        f"than running the {sum(declaring.values())} tests that actually grade the ticket — "
        f"measured, that stub passed every earlier form of this assertion. Its verify is "
        f"{verify!r}"
    )
    seen = sorted(collected_files & set(declaring))
    thinned = [
        f"{module}: {len([n for n in node_ids if n.split('::')[0] == module])} of "
        f"{declaring[module]} graders collected"
        for module in seen
        if len([n for n in node_ids if n.split("::")[0] == module]) < declaring[module]
    ]
    assert not thinned, (
        f"T-112's gate reaches its grader module but collects only part of it: {thinned}. "
        f"Parametrisation only ever inflates the node count above the function count, so a "
        f"shortfall means an `-k`/`--deselect` narrowed the verify down to a token grader — "
        f"which satisfies 'the gate can see them' while still not running them"
    )
    assert seen, (
        f"T-112's recorded gate collects {len(node_ids)} tests from {sorted(collected_files)} "
        f"and NONE of them come from a module that declares itself a T-112 grader. The graders "
        f"are right there — {declaring} — and the gate never reaches them, so every one of "
        f"T-112's four acceptance criteria could be reverted with this gate still green. "
        f"Its verify is {verify!r}"
    )
