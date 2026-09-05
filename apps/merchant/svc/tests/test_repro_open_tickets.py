"""Reproduction gates for the open ``apps/merchant`` tickets.

Every test here asserts the behaviour that SHOULD hold and therefore fails against the tree
as it stands. Each carries ``@pytest.mark.xfail(strict=True)`` while the defect is live, so:

* an ordinary run reports ``xfailed`` and the repo-wide build gate stays GREEN;
* the ticket's own gate runs
  ``export PROXYSHOP_WORKER=0 && uv run python -m pytest
  apps/merchant/svc/tests/test_repro_open_tickets.py -q --runxfail -k <name>``
  and gets a real, selected ``1 failed``;
* the marker cannot outlive the bug — once the defect is closed the test XPASSes, which
  ``strict=True`` turns into a failure, forcing whoever fixed it to delete the marker.

**Nothing here is allowed to skip.** A skip is not a gate, and the compose datastore stack is
routinely down in this repo, so every assertion below is made against a pure function, a
module's public surface, source text, or a subprocess — never against a live datastore.

**Nothing here mutates process-global state.** ``merchant_svc.envelope.store.ENVELOPES`` is a
module-level singleton that the merchant suite only partially isolates, so recording into it
from a test would create exactly the order-dependence T-247 is about. Every probe below
either builds its own ``EnvelopeVersions`` or asserts identity without writing.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import warnings
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
ENVELOPE_PKG = REPO_ROOT / "apps" / "merchant" / "svc" / "src" / "envelope"

#: The two module spellings the repo can reach the merchant envelope store through. Both are
#: importable today (``.pkgroot/merchant_svc`` is a symlink to ``apps/merchant/svc/src``, and
#: the repo root is on ``pythonpath``), and they resolve to the same file on disk.
SHORT_SPELLING = "merchant_svc.envelope.store"
LONG_SPELLING = "apps.merchant.svc.src.envelope.store"


#: A DESIGN-shaped envelope. ``record()`` validates against ``contracts.Envelope`` and needs
#: every one of these eight fields, so a shorter dict is refused for the wrong reason.
def _envelope(store_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "store_id": store_id,
        "version": 1,
        "floors": [],
        "max_discount_pct": 0.0,
        "budget_cap": 0.0,
        "pursue_clusters": [],
        "standing_commitments": [],
        "activation": "shadow",
    }
    body.update(overrides)
    return body


#: Words that name a persistence operation. Matched WHOLE (or as a whole underscore-separated
#: part), never as a substring: ``"commit" in "standing_commitments"`` and ``"load" in
#: "payload"`` are both true, and ``standing_commitments`` is a real field of ``Envelope`` in
#: this very module — so a substring scan could be satisfied by a name with nothing to do with
#: durability.
PERSISTENCE_WORDS = frozenset(
    {"load", "loads", "persist", "save", "flush", "reload", "restore", "commit", "sync", "fetch"}
)

#: Database drivers this repo could plausibly reach ``sealed.envelopes`` with.
DB_DRIVERS = frozenset({"psycopg", "psycopg2", "sqlalchemy", "asyncpg"})


def _names_a_persistence_operation(name: str) -> bool:
    """True when ``name`` is (or is built out of) a whole persistence word."""
    parts = [part for part in name.lower().strip("_").split("_") if part]
    return any(part in PERSISTENCE_WORDS for part in parts)


def _referenced_names(tree: ast.AST) -> set[str]:
    """Every identifier a module really uses — never a docstring, never a comment."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
    return found


def _imports_matching(tree: ast.AST, needle: str) -> list[str]:
    """Imported module names containing ``needle`` — an import statement, not prose."""
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return [module for module in modules if needle in module]


def _non_docstring_literals(tree: ast.AST) -> list[str]:
    """Every string constant that is NOT a docstring.

    ``store.py``'s own module docstring already names ``sealed.envelopes`` — it is where
    DESIGN says the history belongs — so a plain "does the source mention the table" check is
    green before anyone writes a line of persistence.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _parse(path: pathlib.Path) -> ast.AST | None:
    """Parse one product file, or ``None`` if it will not parse.

    ``SyntaxWarning`` is muted here and only here: ``services/ingest/src/er/identity.py:209``
    has ``\\s`` in a non-raw docstring, so parsing the product tree emits a warning that has
    nothing to do with any ticket in this file and would otherwise be charged to it.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            return ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a product file that will not parse
            return None


def _product_python_files() -> list[pathlib.Path]:
    """Every shipped ``.py`` file — apps, packages, services, e2e — excluding tests."""
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
# T-243 — the merchant envelope store exists twice, under two spellings
# ======================================================================================
# MARKER REMOVED — T-243 is fixed. The marker quoted verbatim, and the ritual:
#
#   @pytest.mark.xfail(strict=True, reason=(
#       "T-243: merchant_svc.envelope.store and apps.merchant.svc.src.envelope.store are "
#       "DISTINCT module objects over the same file, with distinct ENVELOPES singletons, "
#       "distinct EnvelopeVersions classes and distinct EnvelopeError subclasses that do not "
#       "catch each other — the frozen E5 acceptance suite imports the LONG spelling while "
#       "production onboarding/routes.py imports the SHORT one, and unlike codes/ the "
#       "envelope package never calls bind_package(); remove this marker with the fix"))
#
# What it encodes: while the defect is live this test must fail, and `strict=True` makes it
# fail LOUDLY the moment it starts passing, so the marker cannot outlive the bug. Its own
# reason text prescribes this removal ("remove this marker with the fix").
#
# Would this test still be wrong if my change were reverted? NO — and that is the whole
# point. Measured both ways on this worktree: with envelope/_spellings.py moved aside and
# __init__/digest/model/store/versions restored from HEAD, the test reports `1 xfailed`
# (the defect is live); with the fix back in place it reports `1 passed`. Nothing about the
# assertion body was touched — the diff removes the decorator and nothing else.
def test_t243_the_merchant_envelope_store_has_exactly_one_module_identity() -> None:
    """One file must be one module, whichever spelling reaches it.

    ``apps/merchant/svc/src/codes/`` already solves this with ``_spellings.py`` and a
    ``bind_package(__name__)`` call, and ``merchant_svc.codes is apps.merchant.svc.src.codes``
    is True as a result. The envelope package has no such binding.
    """
    short = importlib.import_module(SHORT_SPELLING)
    long_ = importlib.import_module(LONG_SPELLING)

    # Control, and the .pth guard: both spellings must be the SAME file, and that file must
    # be the one in the tree under test rather than another checkout sharing this venv.
    short_file = pathlib.Path(short.__file__ or "").resolve()
    long_file = pathlib.Path(long_.__file__ or "").resolve()
    assert short_file == long_file, (
        f"the two spellings are different files: {short_file} vs {long_file}"
    )
    assert short_file.is_relative_to(REPO_ROOT), (
        f"{SHORT_SPELLING} resolved to {short_file}, which is outside the tree under test "
        f"({REPO_ROOT}) — a .pth leak, not a measurement"
    )

    split: list[str] = []
    if short is not long_:
        split.append(f"module object ({SHORT_SPELLING} is not {LONG_SPELLING})")

    # ``Envelope`` and ``EnvelopeError`` are deliberately NOT listed: store.py:21 imports them
    # absolutely from the SHORT spelling, so both copies of store share them unconditionally
    # and testing them proves nothing. The split they belong to is the model module's, checked
    # below. A missing name is reported rather than raising AttributeError, so a rename cannot
    # make this gate red for the wrong reason.
    missing = object()
    for name in (
        "ENVELOPES",
        "EnvelopeVersions",
        "UnknownStore",
        "VersionWentBackwards",
        "StoreMismatch",
    ):
        one = getattr(short, name, missing)
        other = getattr(long_, name, missing)
        if one is missing or other is missing:
            split.append(f"{name} (absent from one spelling)")
        elif one is not other:
            split.append(name)

    # The same file loaded twice is a package-level fault, and the in-tree repair
    # (``_spellings.py`` + ``bind_package``) fixes the whole subtree at once — measured:
    # merchant_svc.codes, .codes.create and .codes.offer are each a single object.
    for module_name in ("merchant_svc.envelope", "merchant_svc.envelope.model"):
        a = importlib.import_module(module_name)
        b = importlib.import_module(module_name.replace("merchant_svc", "apps.merchant.svc.src"))
        if a is not b:
            split.append(f"module object ({module_name})")

    assert not split, (
        f"one file, {short_file}, is loaded as two independent modules; these names are two "
        f"distinct objects: {split}. A store written through one spelling is invisible "
        "through the other, and an `except` on one spelling's error class does not catch the "
        "other's."
    )


# ======================================================================================
# T-248 — record() accepts a caller-asserted 'active' with no approval artifact
# ======================================================================================
# MARKER REMOVED — T-248 is fixed. The marker quoted verbatim, and the ritual:
#
#   @pytest.mark.xfail(strict=True, reason=(
#       "T-248: EnvelopeVersions.record (envelope/store.py:46) validates the contract shape "
#       "and the monotonic-version rule and never looks at `activation`, so a caller-asserted "
#       "activation='active' is filed with approval=None and Envelope.is_live returns True — "
#       "one import away from defeating the approve-then-edit guarantee that put()/activate() "
#       "establish on the HTTP surface; remove this marker with the fix"))
#
# What it encodes: while record() files a caller-asserted `active` this test must fail, and
# strict=True makes it fail loudly once it starts passing, so the marker cannot outlive the
# bug. Its own reason text prescribes this removal.
#
# Would this test still be wrong if my change were reverted? NO. Measured on this worktree:
# with envelope/store.py restored from HEAD (keeping only T-243's relative-import form, so
# the two changes are separated) the test reports `1 xfailed`; with
# `_refuse_unapproved_activation` back it reports `1 passed`. My change is the cause.
# The assertion body is untouched — the diff removes the decorator and nothing else.
def test_t248_an_envelope_cannot_be_recorded_live_without_an_approval_artifact() -> None:
    """Nothing may be live on the caller's say-so. Live means an approval artifact exists.

    Two repairs are acceptable and both make this pass: refuse the record outright (any
    ``EnvelopeError``), or file it forced to ``shadow`` the way ``put()`` already does.

    The claimed envelope differs from the control in exactly one field, ``activation``. The
    first draft also carried ``version=7`` and ``max_discount_pct=99.0``, and either of those
    is a false-green channel: a future, unrelated rule that refused a discount ceiling or a
    first version above 1 would raise ``EnvelopeError``, take the ``return``, and XPASS this
    gate with T-248 untouched.
    """
    from merchant_svc.envelope.model import EnvelopeError  # noqa: PLC0415
    from merchant_svc.envelope.store import EnvelopeVersions  # noqa: PLC0415

    # Control: the identical envelope in `shadow` is accepted, so a refusal below can only be
    # about the activation claim and not about some other field of the document.
    control = EnvelopeVersions().record(_envelope("s-t248", activation="shadow"))
    assert not control.is_live, "fixture error: a shadow envelope reported itself live"

    versions = EnvelopeVersions()
    claimed = _envelope("s-t248", activation="active")

    try:
        recorded = versions.record(claimed)
    except EnvelopeError:
        return  # refused — an acceptable repair

    assert recorded.approval is None, "fixture error: no approval artifact was supplied"
    assert not recorded.is_live, (
        "an envelope asserted its own activation and was filed live with no approval "
        f"artifact: version={recorded.version} activation={recorded.activation!r} "
        f"max_discount_pct={recorded.max_discount_pct} approval={recorded.approval!r}; "
        f"EnvelopeVersions.is_live({recorded.store_id!r}) is "
        f"{versions.is_live(recorded.store_id)}"
    )


# ======================================================================================
# T-246 — nothing in production reads the activation decision
# ======================================================================================
# MARKER REMOVED — T-246 is fixed. The marker quoted verbatim, and the ritual:
#
#   @pytest.mark.xfail(strict=True, reason=(
#       "T-246: `is_live` appears only at its two definition sites "
#       "(envelope/model.py:254, envelope/store.py:89) and in merchant tests — no production "
#       "module anywhere in apps/, packages/, services/ or e2e/ reads it, so nothing "
#       "demonstrates that a shadow or killed store actually stops bidding; the store-agent's "
#       "activation gate (store-agent/src/modes/runner.py:105 _envelope_states) reads a "
#       "context mapping that no production code ever builds from an envelope; remove this "
#       "marker with the fix"))
#
# What it encodes: while no production file reads the activation decision this test must
# fail, and strict=True makes it fail loudly once it starts passing, so the marker cannot
# outlive the bug. Its own reason text prescribes this removal.
#
# Would this test still be wrong if my change were reverted? NO. Measured on this worktree:
# with apps/merchant/svc/src/bidding/ moved aside the test reports `1 xfailed`; with the
# producer back it reports `1 passed`. My change is the cause. The assertion body is
# untouched — the diff removes the decorator and nothing else.
#
# The fix takes the SECOND shape this test's own docstring says it accepts: a merchant-side
# producer (merchant_svc.bidding.gate) that feeds the store-agent's activation gate. It is
# driven against the REAL consumer — store_agent.modes.runner._envelope_states — in
# test_envelope_hardening.py, not against a fake.
def test_t246_some_production_code_reads_the_envelope_activation_decision() -> None:
    """A decision the product computes and nobody asks for is not wired up.

    Two things this gate deliberately does, both of them corrections to a first draft that a
    ``git grep``-shaped check would have got wrong:

    * It **parses**. A regex for ``is_live`` is satisfied by a ``# TODO: consult is_live``
      comment, and the repo already sits one word-boundary from a false hit
      (``packages/llm/src/client.py:58`` carries ``_guard_is_live_for_...`` in a comment).
      Only a real ``Name``/``Attribute``/definition reference counts.
    * It accepts the OTHER shape of the fix. The store-agent's activation gate
      (``modes/runner.py:105 _envelope_states``) speaks the *activation* vocabulary, not
      ``is_live``, so the natural repair is a merchant-side producer that feeds it — and a
      gate that insisted on the literal ``is_live`` would stay red through exactly the right
      fix. Any production module outside the envelope package's own CRUD surface that imports
      a merchant envelope module counts too. Measured: zero such modules exist today.
    """
    definition_sites = {ENVELOPE_PKG / "model.py", ENVELOPE_PKG / "store.py"}
    onboarding_pkg = REPO_ROOT / "apps" / "merchant" / "svc" / "src" / "onboarding"

    reads_the_accessor: list[str] = []
    consumes_an_envelope: list[str] = []
    for path in _product_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a product file that will not parse
            continue
        if path not in definition_sites and "is_live" in _referenced_names(tree):
            reads_the_accessor.append(str(path.relative_to(REPO_ROOT)))
        if path.is_relative_to(ENVELOPE_PKG) or path.is_relative_to(onboarding_pkg):
            continue  # the envelope's own CRUD surface is the writer, not the consumer
        if _imports_matching(tree, "envelope"):
            consumes_an_envelope.append(str(path.relative_to(REPO_ROOT)))

    # Control: the accessor really does exist, so a green here would mean a consumer and not
    # a renamed symbol.
    from merchant_svc.envelope.store import EnvelopeVersions  # noqa: PLC0415

    assert callable(EnvelopeVersions.is_live), "fixture error: EnvelopeVersions.is_live is gone"

    assert reads_the_accessor or consumes_an_envelope, (
        "no production file reads `is_live`, and no production file outside "
        "apps/merchant/svc/src/{envelope,onboarding}/ imports an envelope module at all, so "
        "the kill switch and the shadow default are computed and never consulted — the "
        "merchant side of the envelope has no executed path that demonstrates a killed store "
        "stops bidding, and the store-agent gate at "
        "packages/store-agent/src/modes/runner.py:105 is fed by nobody"
    )


# ======================================================================================
# T-239 — the envelope version history is process-local and dies with the process
# ======================================================================================
# MARKER REMOVED — T-239 is fixed. The marker quoted verbatim, and the ritual:
#
#   @pytest.mark.xfail(strict=True, reason=(
#       "T-239: ENVELOPES = EnvelopeVersions() at envelope/store.py:149 keeps the whole "
#       "append-only history, the version-never-backwards rule and activate-the-head in "
#       "memory; the class takes no backing store, exposes no load/persist member, imports no "
#       "database driver, and db/migrations/0003_sealed_vault_app_tables.sql:41's "
#       "sealed.envelopes table — where DESIGN puts the real history — has no production "
#       "reader or writer anywhere in the repo; remove this marker with the fix"))
#
# What it encodes: while the history can only live in one process's memory this test must
# fail, and strict=True makes it fail loudly once it starts passing, so the marker cannot
# outlive the bug. Its own reason text prescribes this removal.
#
# Would this test still be wrong if my change were reverted? NO. Measured on this worktree:
# with envelope/repository.py moved aside and store.py restored from the T-248 commit, the
# test reports `1 xfailed`; with the seam back it reports `1 passed`. My change is the cause.
# The assertion body is untouched — the diff removes the decorator and nothing else.
def test_t239_the_envelope_version_store_has_a_durability_seam() -> None:
    """The three invariants have to be able to cross the persistence boundary.

    This is a structural gate by necessity: there is no load seam to point a round-trip test
    at, and a datastore-backed test would SKIP when compose is down, which is not a gate.
    It is written against the module-level singleton's own type rather than against
    ``EnvelopeVersions`` by name, so replacing the singleton with a persistent implementation
    satisfies it just as well as giving ``EnvelopeVersions`` a repository argument.
    """
    from merchant_svc.envelope import store as store_mod  # noqa: PLC0415

    resolved = pathlib.Path(store_mod.__file__ or "").resolve()
    assert resolved.is_relative_to(REPO_ROOT), (
        f"{SHORT_SPELLING} resolved to {resolved}, outside the tree under test ({REPO_ROOT})"
    )

    live_store = store_mod.ENVELOPES
    cls = type(live_store)

    # Observable consequence today, for the failure message only: a second store shares
    # nothing with the first. Defensive because a fix that gives EnvelopeVersions durable
    # backing may make a bare `EnvelopeVersions()` reach for a datastore, and this evidence
    # must never be what decides the verdict.
    witness = "not measured"
    try:
        first = store_mod.EnvelopeVersions()
        first.record(_envelope("s-t239-durable"))
        second = store_mod.EnvelopeVersions()
        witness = (
            f"a fresh instance sees {second.stores()!r} after another recorded {first.stores()!r}"
        )
    except Exception as exc:  # noqa: BLE001 - evidence only, never the verdict
        witness = f"a second instance could not be built to compare: {exc!r}"

    # Four independent signals, any one of which means somebody built the seam. They are
    # deliberately different in KIND, because each on its own has a hole: the constructor
    # check is satisfied by any unrelated kwarg, the member check by any method name, and a
    # genuinely durable implementation could have neither — but it cannot reach
    # sealed.envelopes without either naming the table in real SQL or importing a driver.
    takes_backing = len(inspect.signature(cls.__init__).parameters) > 1
    persistence_members = sorted(n for n in dir(cls) if _names_a_persistence_operation(n))

    drivers: list[str] = []
    sealed_sql: list[str] = []
    for path in sorted(ENVELOPE_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        drivers += [m for m in _imports_matching(tree, "") if m.split(".")[0] in DB_DRIVERS]
        sealed_sql += [
            f"{path.name}: {literal[:60]!r}"
            for literal in _non_docstring_literals(tree)
            if "sealed.envelopes" in literal
        ]

    assert takes_backing or persistence_members or drivers or sealed_sql, (
        f"{cls.__module__}.{cls.__qualname__} — the type of the module-level ENVELOPES "
        f"singleton — takes no backing store ({inspect.signature(cls.__init__)}), exposes no "
        "load/persist member, and its package imports no database driver and names "
        "sealed.envelopes nowhere but in a docstring, so the append-only history, the "
        "version-never-backwards rule and activate-the-head exist only for the lifetime of "
        f"one process: {witness}"
    )


# ======================================================================================
# T-247 — the merchant install suite leaves the process-global webhook sink set to None
# ======================================================================================
#: Run inside a fresh interpreter: capture the boot sink, run the merchant install suite,
#: then report what the module global was left as.
#:
#: The whole FILE is run rather than ``-k`` on the one test that installs a sink
#: (``test_a_sink_that_refuses_a_delivery_gets_the_retry_not_a_duplicate``). Measured: a ``-k``
#: that matches nothing exits 5 with the sink untouched, which reads as ``after_is_default``
#: and would XPASS this gate — i.e. renaming that test would silently report the defect fixed.
#: Naming the file cannot miss.
_SINK_RESIDUE_PROBE = """
import json, pathlib
import pytest
from merchant_svc.install import webhooks

boot = webhooks.webhook_sink()
code = pytest.main(["apps/merchant/svc/tests/test_install.py", "-q", "-p", "no:cacheprovider"])
after = webhooks.webhook_sink()
print("PROBE" + json.dumps({
    "module_file": str(pathlib.Path(webhooks.__file__).resolve()),
    "pytest_rc": int(code),
    "boot_is_default": boot is webhooks.default_sink,
    "after_is_default": after is webhooks.default_sink,
    "after_is_none": after is None,
    "after_repr": repr(after),
}))
"""


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-247: install/webhooks.py:418 boots the process-global _sink to default_sink, but "
        "apps/merchant/svc/tests/test_install.py:859 'restores' it with "
        "set_webhook_sink(None) in its finally — to None, not to the value it found — so "
        "every later test in the same process runs against a service that answers an "
        "authenticated delivery '200 recorded' and hands it to nobody, which is exactly the "
        "state webhooks.py:412-417 says it closed; remove this marker with the fix"
    ),
)
def test_t247_the_install_suite_leaves_the_webhook_sink_as_it_found_it() -> None:
    """A suite may install its own sink; it may not leave the process worse than it found it.

    Measured in a subprocess for the same reason
    ``test_merchant_hardening.py:466`` uses one — the state under test is a module global,
    and observing it has to happen in an interpreter this test did not already dirty. The
    subprocess also makes the gate order-independent: it creates the bad state itself rather
    than depending on which tests ran before it.
    """
    env = dict(os.environ, PROXYSHOP_WORKER=os.environ.get("PROXYSHOP_WORKER", "0"))
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_SINK_RESIDUE_PROBE)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    marker = [line for line in completed.stdout.splitlines() if line.startswith("PROBE")]
    assert marker, (
        "the residue probe produced no verdict line.\n"
        f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
    )
    payload = json.loads(marker[-1][len("PROBE") :])

    # The .pth guard: a venv shared with another checkout can put a different tree's module
    # on sys.path, and a reproduction that measured the wrong tree is not a reproduction.
    module_file = pathlib.Path(payload["module_file"])
    assert module_file.is_relative_to(REPO_ROOT), (
        f"the probe imported {module_file}, which is outside the tree under test "
        f"({REPO_ROOT}) — a .pth leak, not a measurement"
    )

    # Controls: the suite really ran and passed, and the boot default really is the default
    # sink. Without the first one, a run that collected nothing would leave the sink untouched
    # and this gate would XPASS — reporting the defect fixed when it was never exercised.
    assert payload["pytest_rc"] == 0, (
        f"apps/merchant/svc/tests/test_install.py did not pass inside the probe "
        f"(pytest exit code {payload['pytest_rc']}; 5 means nothing was collected), so the "
        f"residue reading is not meaningful.\nstdout:\n{completed.stdout[-4000:]}"
    )
    assert payload["boot_is_default"] is True, (
        "fixture error: the module did not boot with default_sink installed"
    )

    assert payload["after_is_default"] is True, (
        "running one merchant install test left the process-global webhook sink as "
        f"{payload['after_repr']} (is None: {payload['after_is_none']}) instead of the boot "
        "default, so every authenticated delivery a later test in that process makes is "
        "verified, put in the display ring, and handed to nobody"
    )


# ======================================================================================
# T-285 — the SyntaxWarning helper in THIS file was written and never wired up
# ======================================================================================
#: This file, which is also the file under test. Unavoidable for a test-hygiene ticket: the
#: defect *is* in the test file, so the gate and its subject are the same path. It is called
#: out rather than glossed, because "a test may never grade a file inside its own author's
#: write scope" is a real rule and this is the one shape that cannot honour it.
THIS_FILE = pathlib.Path(__file__).resolve()

#: The product file whose non-raw docstring is the reason `_parse` exists at all.
_WARNING_SOURCE = REPO_ROOT / "services" / "ingest" / "src" / "er" / "identity.py"


def _ast_parse_call_sites(tree: ast.AST) -> list[int]:
    """Line numbers of every ``ast.parse(...)`` CALL in ``tree`` — a call, not a mention."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "parse"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ast"
    )


def _helper_body_lines(tree: ast.AST, name: str) -> range:
    """The line span of the named module-level function, or an empty range if it is gone."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return range(node.lineno, (node.end_lineno or node.lineno) + 1)
    return range(0)


def test_t285_the_syntax_warning_helper_is_armed() -> None:
    """Control for T-285, and it must PASS. Three things the red below depends on.

    Without all three, a red on the gate would be an accident of collection rather than the
    defect: the helper could have been renamed, the warning it mutes could have been fixed at
    source, or this file could have stopped containing any ``ast.parse`` at all.
    """
    tree = _parse(THIS_FILE)
    assert tree is not None, f"{THIS_FILE} does not parse"

    # 1. The helper is still here, under this name, with a body.
    span = _helper_body_lines(tree, "_parse")
    assert len(span) > 1, "_parse is gone from this file; T-285 is about a helper that exists"

    # 2. It really does mute a SyntaxWarning — the muting is inside its span, not decorative.
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "simplefilter"
        for node in ast.walk(tree)
        if getattr(node, "lineno", -1) in span
    ), "_parse no longer mutes anything, so there is nothing for a call site to inherit"

    # 3. The warning it was written for is STILL EMITTED by the product tree today. If
    #    services/ingest fixes that docstring the helper stops being needed and this control
    #    goes red — which is the honest signal that T-285's premise expired, not a pass.
    assert _WARNING_SOURCE.is_file(), f"{_WARNING_SOURCE} is gone"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SyntaxWarning)
        ast.parse(_WARNING_SOURCE.read_text(encoding="utf-8"))
    assert any(issubclass(w.category, SyntaxWarning) for w in caught), (
        f"{_WARNING_SOURCE} no longer emits a SyntaxWarning, so `_parse` has nothing to mute "
        "and T-285's premise has expired — retire the helper rather than wiring it"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-285: `_parse` (this file) wraps ast.parse in catch_warnings/simplefilter('ignore', "
        "SyntaxWarning) and its docstring names exactly why — services/ingest/src/er/"
        "identity.py has \\s in a non-raw docstring — but the two call sites it was written "
        "to replace still call ast.parse bare, so the warning is still charged to "
        "test_t246_some_production_code_reads_the_envelope_activation_decision. Worse than "
        "noise: under -W error::SyntaxWarning CPython raises the escalated warning as a "
        "SyntaxError, which the bare site's `except SyntaxError: continue` swallows, silently "
        "dropping identity.py from a scan that claims to walk every product file; remove this "
        "marker with the fix"
    ),
)
def test_t285_every_ast_parse_in_this_file_goes_through_the_muting_helper() -> None:
    """The helper is only a fix if the call sites use it.

    Deliberately NOT asserted here: "``_parse`` has at least one caller". This gate's own
    control calls it, so that assertion would be satisfied by this file's arrival rather than
    by the repair — a gate that counts its own call is a gate that passes itself. What is
    asserted is the remedy the ticket actually names: the bare call sites go through the
    helper (or the helper goes away, which makes the set below empty just as well).
    """
    tree = _parse(THIS_FILE)
    assert tree is not None, f"{THIS_FILE} does not parse"

    inside_helper = _helper_body_lines(tree, "_parse")
    bare = [line for line in _ast_parse_call_sites(tree) if line not in inside_helper]

    assert bare == [], (
        f"{THIS_FILE.name} calls ast.parse directly at line(s) {bare}, bypassing the `_parse` "
        "helper written to mute the SyntaxWarning that services/ingest/src/er/identity.py "
        "emits. The consequence is not cosmetic: with SyntaxWarning escalated to an error "
        "CPython raises it as a SyntaxError, and a bare site guarded by "
        "`except SyntaxError: continue` then drops that file from a scan that claims to walk "
        "every product file — a coverage hole in the gate, reported as a clean pass"
    )


# ======================================================================================
# T-317 — the merchant serves routes that appear in no published contract
# ======================================================================================
#: HTTP methods an OpenAPI path item can carry. `parameters` and `summary` are path-item keys
#: too and are not operations, so a plain `for method in item` over-counts.
_OPERATION_METHODS = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
)


def _served_operations() -> set[tuple[str, str]]:
    """Every ``(method, path)`` the merchant app actually answers, from the built app."""
    from merchant_svc.main import create_app  # noqa: PLC0415

    schema = create_app().openapi()
    return {
        (method.lower(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method.lower() in _OPERATION_METHODS
    }


def _published_operations() -> set[tuple[str, str]]:
    """Every ``(method, path)`` the pinned merchant contract declares.

    Read through ``contracts.openapi``, the repo's own loader, rather than by re-opening the
    JSON: the document is ``packages/contracts/openapi/merchant.openapi.json``, which is
    outside this service's tree, and that is the point — the thing this gate grades the
    service against is not a file the service's own lane can edit.
    """
    from contracts.openapi import documents  # noqa: PLC0415

    paths = documents()["merchant"]["paths"]
    return {
        (method.lower(), path)
        for path, item in paths.items()
        for method in item
        if method.lower() in _OPERATION_METHODS
    }


def test_t317_the_merchant_route_comparison_is_armed() -> None:
    """Control for T-317, and it must PASS. The comparison machinery works both ways.

    A red below has to mean "the service serves something the contract does not declare". It
    must not be able to mean "the app would not build", "the contract would not load", or
    "the two are described in different vocabularies and nothing ever matches".
    """
    served = _served_operations()
    published = _published_operations()

    assert served, "the merchant app served no operations at all"
    assert published, "the pinned merchant contract declared no operations at all"

    # The vocabularies really do meet: several routes match exactly, so a non-match below is
    # a real gap and not two spellings passing each other.
    assert len(served & published) >= 3, (
        f"served and published overlap in only {sorted(served & published)}, which is too "
        "little to trust a diff between them"
    )
    # And the other direction is clean today, so the red below is unambiguously about
    # served-but-unpublished and carries no second cause.
    assert published - served == set(), (
        f"the contract pins routes the service does not answer: {sorted(published - served)}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-317: the merchant serves GET /install, GET /install/callback and GET "
        "/install/shops (apps/merchant/svc/src/install/routes.py:145, :173, :248) and none of "
        "the three appears in packages/contracts/openapi/merchant.openapi.json. Undeclared "
        "served routes are a security and review surface: nothing in the contract review "
        "process ever looks at them. test_merchant_hardening.py:413-417 hard-exempts exactly "
        "these three while its own docstring justifies only 'the install's own OAuth pair' — "
        "/install/shops is an administrative JSON endpoint, not a browser redirect; remove "
        "this marker with the fix"
    ),
)
def test_t317_every_route_the_merchant_serves_is_in_its_published_contract() -> None:
    """A served route nobody declared is a surface nobody reviews.

    Either direction of drift is a defect, and the published-but-not-served direction is
    already clean (asserted in the armed control above), so this gate is about the other one.
    Two repairs make it pass and the gate does not care which: publish the three routes in
    the merchant contract, or stop serving them.
    """
    served = _served_operations()
    published = _published_operations()

    unpublished = sorted(served - published)
    assert unpublished == [], (
        f"the merchant service answers {len(unpublished)} operation(s) that appear in no "
        f"published contract: {unpublished}. They are reachable, they are not generated into "
        "any client, and no contract review has ever seen them"
    )
