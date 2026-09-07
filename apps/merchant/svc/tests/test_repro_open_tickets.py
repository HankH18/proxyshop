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
import re
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
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

    This used to mute ``SyntaxWarning`` as well, because
    ``services/ingest/src/er/identity.py`` carried ``\\s`` in a non-raw docstring and the
    warning would have been charged to whichever ticket in this file happened to walk the
    product tree. That docstring is now raw, and a sweep of all 368 shipped modules emits
    **zero** ``SyntaxWarning``, so there is nothing left to mute — T-285's premise expired
    and its control test said in as many words to retire the helper rather than wire it.

    The muting is not reinstated on the next one, either. A warning that surfaces is a
    defect someone can fix; a warning this helper swallows is a defect nobody sees, and the
    file it comes from is named in the traceback either way.
    """
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
        # T-285: through `_parse`, not a bare `ast.parse`. The inline `except SyntaxError:
        # continue` this replaces was not equivalent — under `-W error::SyntaxWarning` the
        # identity.py:209 warning is raised AS a SyntaxError, so the bare site dropped that
        # file from a scan that claims to walk every product file and still reported a pass.
        tree = _parse(path)
        if tree is None:  # pragma: no cover - a product file that will not parse
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
        # T-285: through `_parse`. This site never swallowed anything — a bare `ast.parse`
        # here would have raised — so the added assertion keeps that loudness rather than
        # letting `_parse`'s `None` return quietly skip an envelope module.
        tree = _parse(path)
        assert tree is not None, f"fixture error: {path} does not parse"
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


# MARKER REMOVED — T-247 is fixed. The marker quoted verbatim, and the ritual:
#
#   @pytest.mark.xfail(strict=True, reason=(
#       "T-247: install/webhooks.py:418 boots the process-global _sink to default_sink, but "
#       "apps/merchant/svc/tests/test_install.py:859 'restores' it with "
#       "set_webhook_sink(None) in its finally — to None, not to the value it found — so "
#       "every later test in the same process runs against a service that answers an "
#       "authenticated delivery '200 recorded' and hands it to nobody, which is exactly the "
#       "state webhooks.py:412-417 says it closed; remove this marker with the fix"))
#
# What it encodes: while one install test can leave the process-global sink unwired this
# test must fail, and strict=True makes it fail loudly once it starts passing, so the marker
# cannot outlive the bug. Its own reason text prescribes this removal.
#
# Would this test still be wrong if my change were reverted? NO. Measured on this worktree:
# with install/webhooks.py and install/__init__.py restored from HEAD the test reports
# `1 xfailed`; with the fix back it reports `1 passed`. My change is the cause.
# The assertion body is untouched — the diff removes the decorator and nothing else.
#
# The repair is production-side, where the ticket's scope puts it: set_webhook_sink(None) now
# restores the boot default instead of clearing the sink, and "hand deliveries to nobody" is
# spelled inbox_only_sink. test_install.py:859 was NOT edited.
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
# T-317 — the merchant serves routes that appear in no published contract
# ======================================================================================
#: HTTP methods an OpenAPI path item can carry. `parameters` and `summary` are path-item keys
#: too and are not operations, so a plain `for method in item` over-counts.
_OPERATION_METHODS = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
)


#: Endpoints FastAPI mounts for itself. Excluded by exact path because they are the
#: framework's, not the merchant's — no contract review is owed them and no ticket is about
#: them. Listed rather than pattern-matched so a real route can never fall through by
#: resembling one.
_FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


def _normalize_path(path: str) -> str:
    """A route path in the contract's spelling, converter suffixes removed.

    Starlette keeps the converter in the raw path — ``install/routes.py`` declares
    ``/webhooks/shopify/{topic:path}`` — while OpenAPI (and therefore the pinned contract)
    spells the same parameter ``{topic}``. Comparing the raw strings would report the one
    route both sides DO agree on as a mismatch, which is a false red, not a finding.
    """
    return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)


def _served_operations() -> set[tuple[str, str]]:
    """Every ``(method, path)`` the merchant app actually answers.

    Read off ``app.routes`` and NOT off ``app.openapi()``. The schema is what the service
    *documents*, so a route carrying ``include_in_schema=False`` is invisible to it — and this
    gate exists precisely to find routes nothing reviews. Grading the schema would have made
    one keyword argument a green button for a ticket about undeclared surface. Measured: the
    app answers 200 on four framework endpoints the schema never lists, which is how the hole
    was found.
    """
    from merchant_svc.main import create_app  # noqa: PLC0415

    served: set[tuple[str, str]] = set()

    def walk(routes: Any) -> None:
        # This FastAPI version wraps each `include_router` in a `_IncludedRouter` that carries
        # no `.path` of its own and holds the real routes in a nested `.routes`. A flat scan
        # of `app.routes` therefore sees ONLY the four framework endpoints and reports the
        # service as serving nothing — measured, and caught by this gate's armed control,
        # which is exactly what that control is for.
        for route in routes or ():
            # `_IncludedRouter` exposes the router it wrapped as `original_router`, not as
            # `routes`; both spellings are followed so this survives a FastAPI upgrade in
            # either direction.
            wrapped = getattr(route, "original_router", None)
            nested = getattr(route, "routes", None) or getattr(wrapped, "routes", None)
            if nested:
                walk(nested)
                continue
            raw = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not raw or not methods or raw in _FRAMEWORK_PATHS:
                continue
            path = _normalize_path(raw)
            served.update(
                (method.lower(), path) for method in methods if method.lower() in _OPERATION_METHODS
            )

    walk(create_app().routes)
    return served


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


# T-317 CLOSED — the `xfail(strict=True)` marker that stood here is REMOVED in the change that
# closed it. The assertion is untouched.
#
# The repair chosen was PUBLISH rather than "stop serving them", and the reason is that all
# three are load-bearing: `GET /install` and `GET /install/callback` are the Shopify OAuth pair
# a merchant's browser walks to onboard at all, and `GET /install/shops` is the administrative
# read of who is installed. Deleting any of them removes onboarding; there was never a version
# of this ticket where the routes were the thing that was wrong.
#
# What WAS wrong is that they were reachable and undeclared, and the standing justification for
# that — `test_merchant_hardening.py`'s docstring, "a Shopify-facing browser redirect, not a
# cross-domain service API" — is an argument about who calls a route, not about whether anyone
# reviews it. It also never covered `GET /install/shops`, which returns JSON, enumerates every
# installed merchant, and is guarded by a bearer token; the exemption listed it anyway. All
# three now appear in packages/contracts/openapi/merchant.openapi.json with the refusals they
# actually answer (400/401/422/502/503), each captured by DRIVING the built app.
#
#   BEFORE (this branch, worker 2, --runxfail):
#     "the merchant service answers 3 operation(s) that appear in no published contract:
#      [('get', '/install'), ('get', '/install/callback'), ('get', '/install/shops')]"
#   AFTER: served 9, published 9; `served - published` empty and `published - served` empty.
#   CAUSATION: deleting the three new entries from the merchant contract returns this node to
#     the BEFORE failure verbatim.
#
# BLAST RADIUS, checked before moving: `test_merchant_hardening.py:411-423` computes
# `served - pinned - exempt` and asserts it empty. Adding the three to PINNED_ROUTES empties
# `served - pinned` first, so the assertion still holds and its `exempt` set is now redundant
# rather than wrong. That file belongs to another lane and is left alone.
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


# ======================================================================================
# T-345 — the merchant money path takes a price and a currency it cannot read
# ======================================================================================
#: The instant every T-345 probe is anchored at. Pinned rather than read from the clock so
#: the D22 window (``min(now + 48h, offer.expires_at)``) is the same on every machine at
#: every moment; the fixture offer expires six hours later, which is inside the 48h cap.
T345_NOW = datetime(2026, 1, 1, tzinfo=UTC)


#: A well-formed accepted offer for the merchant's ``POST /codes`` path, in the flat spelling
#: the exchange actually posts (``discount_pct``/``variant_id``). 10 percentage points off a
#: 100.00 list price is 90.00, so the T-203 price-versus-percentage cross-check in
#: ``merchant_svc.codes.offer.assert_discount_matches_prices`` has two readable prices and
#: agrees with them — every red below is therefore about the ONE field a probe overrides.
def _mintable_offer(**overrides: Any) -> dict[str, Any]:
    offer: dict[str, Any] = {
        "store_id": "acme",
        "offer_id": "o-t345",
        "discount_pct": 10.0,
        "discount_type": "percentage",
        "variant_id": "1001",
        "expires_at": "2026-01-01T06:00:00Z",
        "list_price": "100.00",
        "unit_price": "90.00",
        "currency": "USD",
    }
    offer.update(overrides)
    return offer


#: A fixed-amount discount, which is the only shape that reads ``currency`` at all.
def _fixed_amount_offer(**overrides: Any) -> dict[str, Any]:
    offer: dict[str, Any] = {
        "store_id": "acme",
        "offer_id": "o-t345-fixed",
        "discount": {"type": "fixed_amount", "value": "5.00"},
        "currency": "USD",
        "variant_id": "1001",
        "expires_at": "2026-01-01T06:00:00Z",
    }
    offer.update(overrides)
    return offer


def test_t345_the_merchant_money_path_control_is_armed() -> None:
    """Control for T-345, and it must PASS. Three things the red below depends on.

    Without all three a red on the gate would be an accident rather than the defect: the
    fixture offer could have stopped being mintable at all (then *every* probe raises and the
    gate passes for the wrong reason), the price-versus-percentage cross-check could have
    stopped being able to refuse anything, or ``currency`` could have stopped being read.
    """
    from merchant_svc.codes.offer import (  # noqa: PLC0415
        UnusableOffer,
        assert_offer_is_mintable,
        offer_discount,
    )

    # 1. The fixture really is mintable, so a refusal below is about the overridden field.
    assert_offer_is_mintable("acme", _mintable_offer(), now=T345_NOW)
    assert offer_discount(_mintable_offer()).percent == 10.0

    # 2. The cross-check can refuse: the same two prices against a percentage they contradict
    #    (0.2 where 20.0 was meant — the two-orders-of-magnitude unit error T-203 is about).
    with pytest.raises(UnusableOffer):
        assert_offer_is_mintable("acme", _mintable_offer(discount_pct=0.2), now=T345_NOW)

    # 3. `currency` is really read off a fixed-amount discount, so the probe below has a
    #    field to be wrong about.
    assert offer_discount(_fixed_amount_offer()).currency == "USD"


# MARKER REMOVED — T-345 is fixed. The marker quoted verbatim, and the ritual:
#
#   @pytest.mark.xfail(strict=True, reason=(
#       "T-345: on the merchant's money path a price the system cannot read is treated as a "
#       "price the offer never stated. `_money` (apps/merchant/svc/src/codes/offer.py:348) "
#       "answers None for anything it cannot parse, and `assert_discount_matches_prices` "
#       "reads that None as 'the offer states only one price, so there is nothing to check' "
#       "and returns silently — so an offer whose unit_price is an object, a dict, a list, "
#       "'abc', True or a negative number is ACCEPTED and a real single-use discount is "
#       "minted, with the one guard that catches a 0.2 meant as 20% disabled. In the same "
#       "function `offer_discount` takes a non-string currency through `str(currency)`, so "
#       "`currency=object()` denominates a money amount in '<object object at 0x...>' — a "
#       "value taken and coerced rather than refused, and a heap address, on a path whose "
#       "UnusableOffer text is echoed to the caller as POST /codes' 400 detail "
#       "(codes/routes.py:139); remove this marker with the fix"))
#
# What it encodes: while the money path takes a price it cannot read this test must fail, and
# strict=True makes it fail loudly once it starts passing, so the marker cannot outlive the
# bug. Its own reason text prescribes this removal.
#
# Would this test still be wrong if my change were reverted? YES. Measured on this worktree:
# with codes/offer.py restored from HEAD the selected test reports `1 failed` under
# `--runxfail` with `{'unit_price': ["'object'", "'dict'", "'list'", "'str'", "'bool'",
# "'str'"], 'list_price': [...same...]}`; with `_stated_price` and `_currency` in place it
# reports `1 passed`. My change is the cause. The assertion body is untouched — the diff
# removes the decorator and nothing else.
def test_t345_the_merchant_money_path_refuses_money_it_cannot_read() -> None:
    """A money field the system cannot interpret is refused, never treated as absent.

    Absent and unreadable are different answers. Absent is legitimate — an offer that states
    only a list price has nothing to cross-check — but a field that is *present* and
    unreadable is a value the system cannot confidently interpret, and on a path that mints a
    real spendable discount the failure mode of guessing is charging the wrong amount.
    """
    from merchant_svc.codes.offer import UnusableOffer, offer_discount  # noqa: PLC0415
    from merchant_svc.codes.offer import assert_offer_is_mintable as mintable  # noqa: PLC0415

    # (label, value). Labelled rather than repr'd in the report below, because `repr` of the
    # bare object renders a live heap address and this message is read by people.
    unreadable: list[tuple[str, Any]] = [
        ("an object", object()),
        ("a dict", {"amount": "90.00"}),
        ("a list", ["90.00"]),
        ("the string 'abc'", "abc"),
        ("the boolean True", True),
        ("the negative price '-5.00'", "-5.00"),
    ]
    taken: dict[str, list[str]] = {"unit_price": [], "list_price": []}
    for field in taken:
        for label, value in unreadable:
            try:
                mintable("acme", _mintable_offer(**{field: value}), now=T345_NOW)
            except UnusableOffer:
                continue
            taken[field].append(label)
    refused = taken
    assert refused == {"unit_price": [], "list_price": []}, (
        f"the merchant minted a code for an offer whose stated price it could not read: "
        f"{refused}. `_money` answered None, `assert_discount_matches_prices` read that as "
        "'the offer states no such price' and skipped the T-203 unit check entirely"
    )

    # A non-string currency is refused rather than coerced. `str(object())` renders a live
    # heap address, and that address then denominates a real money amount.
    for bad_currency in (object(), 123, ["USD"], {"code": "USD"}, True):
        with pytest.raises(UnusableOffer):
            offer_discount(_fixed_amount_offer(currency=bad_currency))


# ======================================================================================
# T-350 — sealed.envelopes can hold an activation the domain rule forbids
# ======================================================================================
#: The migration that declares every ``sealed.*`` table. T-350 names line 41 of this file.
SEALED_MIGRATION = REPO_ROOT / "db" / "migrations" / "0003_sealed_vault_app_tables.sql"

#: Words a column recording the written approval artifact would be built out of. Matched as
#: whole underscore-separated parts of a column name, never as substrings — the same rule
#: :data:`PERSISTENCE_WORDS` follows above, and for the same reason.
APPROVAL_COLUMN_WORDS = frozenset({"approval", "approved", "approver"})

#: DDL keywords that start a table *constraint* rather than a column definition.
_DDL_CONSTRAINT_KEYWORDS = ("constraint", "primary", "check", "unique", "foreign", "exclude")

#: The three parts of :class:`merchant_svc.envelope.model.ApprovalArtifact` that R6 makes
#: mandatory — who approved, when, and *what* (the digest the approval is bound to). ``note``
#: and ``document_ref`` are optional on the artifact and are optional here too.
REQUIRED_APPROVAL_PARTS = ("approver", "approved_at", "envelope_hash")


def _sealed_envelopes_ddl() -> str:
    """The body of ``CREATE TABLE ... sealed.envelopes ( ... )``, comments stripped.

    Comments are stripped because this whole file is heavily commented and a scan that reads
    prose would go green on a paragraph *describing* the missing column.
    """
    sql = SEALED_MIGRATION.read_text(encoding="utf-8")
    start = sql.find("sealed.envelopes")
    if start < 0:
        return ""
    body = sql[sql.find("(", start) + 1 : sql.find(");", start)]
    return "\n".join(line.split("--", 1)[0] for line in body.splitlines())


def _ddl_items() -> list[str]:
    """The table body split into its top-level items — one column or constraint each.

    The body is split on commas **at nesting depth zero**, not line by line: a multi-line
    ``CHECK (max_discount_pct IS NULL OR (...))`` puts column names on continuation lines, and
    a line-wise reader reports them as columns of their own.
    """
    body = _sealed_envelopes_ddl()
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(character)
    items.append("".join(current))
    return items


def _sealed_envelopes_columns() -> list[str]:
    """Every column ``sealed.envelopes`` declares, in declaration order."""
    columns: list[str] = []
    for item in _ddl_items():
        token = item.split(maxsplit=1)[0].lower() if item.split() else ""
        if not token or token.startswith(_DDL_CONSTRAINT_KEYWORDS):
            continue
        columns.append(token)
    return columns


def _sealed_envelopes_checks() -> dict[str, str]:
    """``{constraint name: its CHECK expression}``, whitespace-collapsed and lowercased.

    Only *named* table constraints are returned, which is every constraint this table has.
    Reading the expression rather than searching the whole DDL for keywords is the point: a
    scan over the file's text goes green when the words it wants appear in two unrelated
    constraints, and "the activation is tied to the approval" is a claim about ONE expression.
    """
    checks: dict[str, str] = {}
    for item in _ddl_items():
        collapsed = " ".join(item.split()).lower()
        match = re.match(r"constraint\s+(\w+)\s+check\s*\((.*)\)\s*$", collapsed, re.DOTALL)
        if match:
            checks[match.group(1)] = match.group(2)
    return checks


def test_t350_the_sealed_envelopes_reader_is_armed() -> None:
    """Control for T-350, and it must PASS. The DDL reader really reads that table.

    A red below has to mean "the table cannot record an approval". It must not be able to
    mean "the migration moved", "the CREATE TABLE block was not found", or "the extraction
    returned an empty string and every scan over it is vacuous".
    """
    from merchant_svc.envelope.model import ACTIVE  # noqa: PLC0415

    assert SEALED_MIGRATION.is_file(), f"{SEALED_MIGRATION} is gone; T-350 names that file"

    columns = _sealed_envelopes_columns()
    # The three columns the merchant repository writes on every persist are all found, so the
    # extraction is really parsing a column list and not an empty string.
    assert {"store_id", "version", "activation"} <= set(columns), (
        f"the sealed.envelopes DDL reader found columns {columns}, which does not include the "
        "three merchant_svc.envelope.repository.persist writes; the reader is broken, not the "
        "schema"
    )
    # And the CHECK that admits the forbidden value is really there to be found.
    assert f"'{ACTIVE}'" in _sealed_envelopes_ddl(), (
        f"the sealed.envelopes DDL no longer mentions {ACTIVE!r} at all, so there is nothing "
        "for the gate below to be about"
    )
    # The constraint reader is armed too: it finds the constraints this table has always had,
    # so a red below cannot mean "the CHECK parser returned an empty dict".
    checks = _sealed_envelopes_checks()
    assert {"envelopes_activation_check", "envelopes_discount_range"} <= set(checks), (
        f"the sealed.envelopes CHECK reader found {sorted(checks)}, which does not include the "
        "two constraints that have been on this table since it was created; the reader is "
        "broken, not the schema"
    )
    assert f"'{ACTIVE}'" in checks["envelopes_activation_check"], (
        "the CHECK reader returned an expression that does not contain the value that "
        "constraint exists to admit, so its expressions are not being read"
    )


# TEST EDIT — the `xfail(strict=True)` marker that stood here was REMOVED, and nothing below
#             it was weakened; the assertions were made STRICTER in the same change. The
#             marker said the fix was "out of apps/merchant's file scope"; it no longer is.
#             db/migrations/0003_sealed_vault_app_tables.sql now declares approval_approver,
#             approval_approved_at, approval_envelope_hash, approval_note and
#             approval_document_ref, plus `envelopes_active_requires_approval`, which is the
#             CHECK the gate below now insists on reading as a single binding expression
#             rather than as keywords scattered across the file. Under `strict=True` leaving
#             the marker would turn the repaired defect into a RED suite.
def test_t350_the_envelope_table_cannot_hold_an_activation_the_rule_forbids() -> None:
    """A domain rule the storage layer does not share is one bug away from being no rule.

    The forbidden transition is precise and it is written down twice: R6/T-248 say a version
    is ``active`` only while a written approval artifact bound to *those very terms* is on
    file, and ``merchant_svc.envelope.store._refuse_unapproved_activation`` enforces it on
    every write. The table must enforce the half it can — that the record EXISTS — because a
    rule only application code knows is one bug in that code away from being no rule.

    Two repairs satisfy this and the gate does not care which: give the table the approval
    columns AND a CHECK that makes ``activation = 'active'`` require them, or remove
    ``'active'`` from ``envelopes_activation_check`` so the storage layer stops claiming to
    hold a state it cannot justify. The first is what landed.

    What the gate will NOT accept, measured against hand-built variants of the migration: the
    columns present with no binding CHECK; a CHECK that demands only the approver, so half an
    artifact still admits a live row; or a table with no ``approval_envelope_hash`` column, so
    an approval is recorded but bound to nothing.
    """
    from merchant_svc.envelope.model import ACTIVE  # noqa: PLC0415

    ddl = _sealed_envelopes_ddl()
    columns = _sealed_envelopes_columns()
    approval_columns = [
        column
        for column in columns
        if APPROVAL_COLUMN_WORDS & {part for part in column.split("_") if part}
    ]

    if not approval_columns:
        assert f"'{ACTIVE}'" not in ddl, (
            f"sealed.envelopes has no column recording a written approval — its columns are "
            f"{columns} — yet its activation CHECK admits {ACTIVE!r}. The table can therefore "
            f"hold a live envelope that R6 and merchant_svc.envelope.store."
            f"_refuse_unapproved_activation both say is impossible, and the durability seam "
            f"has to downgrade every restored {ACTIVE!r} row to shadow because the artifact "
            f"that would justify it was never storable"
        )
        return

    # The columns exist — then every mandatory part of the artifact needs one, because an
    # approval missing its approver, its date or the digest it is bound to is not a record.
    recorded: dict[str, str] = {}
    for part in REQUIRED_APPROVAL_PARTS:
        carriers = [column for column in approval_columns if column.endswith(part)]
        assert carriers, (
            f"sealed.envelopes has approval columns {approval_columns} but none of them "
            f"records the artifact's {part!r}; ApprovalArtifact.parse refuses an artifact "
            f"without it, so a row that cannot carry it cannot justify {ACTIVE!r} either"
        )
        recorded[part] = carriers[0]

    # ...and a CHECK has to make them mandatory for an active row, because a nullable column
    # nothing checks permits exactly the same forbidden row the ticket is about. The binding
    # constraint must be ONE expression naming the activation, the live value, and a NOT NULL
    # test on every mandatory part — three separate constraints that each mention one of them
    # do not compose into the implication.
    binding = [
        name
        for name, expression in _sealed_envelopes_checks().items()
        if "activation" in expression
        and f"'{ACTIVE}'" in expression
        and all(
            re.search(rf"\b{re.escape(column)}\s+is\s+not\s+null", expression)
            for column in recorded.values()
        )
    ]
    assert binding, (
        f"sealed.envelopes records the approval in {approval_columns}, but no single CHECK "
        f"ties it to the activation: a row with activation = {ACTIVE!r} and every approval "
        f"column NULL is still valid at the storage layer, which is the state the domain rule "
        f"forbids. The constraints read were {sorted(_sealed_envelopes_checks())}"
    )


#: The repo's own approved-envelope corpus. Real terms, authored for the acceptance suite and
#: not by this test — a hand-written payload is written by the same mind that chose the bound.
ENVELOPE_FIXTURES = REPO_ROOT / "fixtures" / "envelopes"


class _RoundTrip:
    """A connection that keeps what ``persist`` sends and replays it as ``load`` rows.

    **Not a database, and it does not pretend to be.** It proves the repository writes every
    part of the approval artifact into the row and rebuilds the same artifact from it — the
    boundary T-350 is about. That the *table* refuses the forbidden row is the schema gate
    above; that Postgres accepts these statements is not in evidence here, and this file may
    not reach a datastore (see the module docstring).

    Keyed on ``(store_id, version)`` and last-write-wins, because that is what
    ``on conflict (store_id, version) do update`` means: activate and kill do not bump the
    version, so they restate v1's row rather than appending to it.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, Any], tuple[Any, ...]] = {}
        self.commits = 0

    class _Cursor:
        def __init__(self, connection: _RoundTrip) -> None:
            self._connection = connection

        def __enter__(self) -> _RoundTrip._Cursor:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            if statement.lower().startswith("insert"):
                self._connection.rows[parameters[0], parameters[1]] = parameters

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [self._connection.rows[key] for key in sorted(self._connection.rows)]

    def cursor(self) -> _RoundTrip._Cursor:
        return _RoundTrip._Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


def test_t350_an_approved_activation_survives_the_persistence_boundary() -> None:
    """R6, durably: the approval that authorized a live envelope is written down and read back.

    The schema gate above is about what the table may HOLD. This is the other half and the
    reason the columns are worth adding at all: an envelope a merchant really approved and
    really activated must come back live after a restart, carrying the same artifact. Today it
    does not — ``merchant_svc.envelope.repository`` has nowhere to put the artifact, so
    ``restorable`` downgrades the row to shadow and the store silently stops bidding.

    Driven with ``fixtures/envelopes/store-alpha.approved.json``: real terms with two floors, a
    cap, a budget and two standing commitments, authored for the acceptance suite rather than
    for this assertion.
    """
    from merchant_svc.envelope.digest import approval_digest  # noqa: PLC0415
    from merchant_svc.envelope.model import ACTIVE  # noqa: PLC0415
    from merchant_svc.envelope.repository import PostgresEnvelopeRepository  # noqa: PLC0415
    from merchant_svc.envelope.store import EnvelopeVersions  # noqa: PLC0415

    fixture = json.loads(
        (ENVELOPE_FIXTURES / "store-alpha.approved.json").read_text(encoding="utf-8")
    )
    terms = fixture["envelope"]
    store_id = terms["store_id"]

    connection = _RoundTrip()
    versions = EnvelopeVersions(PostgresEnvelopeRepository(connection))
    stored = versions.put(store_id, terms)
    approval = {
        "approver": "owner@store-alpha.example",
        "approved_at": "2026-09-05T10:00:00+00:00",
        "envelope_hash": approval_digest(stored),
        "note": "approved on the onboarding call",
        "document_ref": "s3://approvals/store-alpha-v1.pdf",
    }
    live = versions.activate(store_id, approval)
    assert live.activation == ACTIVE, "the fixture did not activate; the test is broken"

    # The restart: a brand-new store over the same rows, sharing nothing else.
    restarted = EnvelopeVersions(PostgresEnvelopeRepository(connection))
    head = restarted.current(store_id)

    assert restarted.is_live(store_id) is True, (
        "an envelope the merchant approved and activated came back not live: the approval "
        "artifact did not survive the persistence boundary, so restorable() downgraded it to "
        "shadow and the store stops bidding across a restart (R6)"
    )
    assert head.approval is not None, "the restored live envelope carries no approval artifact"
    assert head.approval.approver == "owner@store-alpha.example"
    assert head.approval.approved_at == "2026-09-05T10:00:00+00:00"
    assert head.approval.note == "approved on the onboarding call"
    assert head.approval.document_ref == "s3://approvals/store-alpha-v1.pdf"
    # And it is still bound to THESE terms — a hash that survived as text but no longer covers
    # the row it came back on would be an approval of a document nobody approved.
    assert head.approval.envelope_hash == approval_digest(head)
    # The terms themselves round-tripped: this is the positive control the schema change must
    # not break.
    assert head.max_discount_pct == terms["max_discount_pct"]
    assert head.budget_cap == terms["budget_cap"]
    assert len(head.floors) == len(terms["floors"])
    assert len(head.standing_commitments) == len(terms["standing_commitments"])


def test_t350_a_row_with_no_recorded_approval_still_comes_back_in_shadow() -> None:
    """The fail-safe direction, kept. A live row with no artifact must not be believed.

    This is the half of the old behaviour that must survive the fix: adding the columns makes
    an approved activation storable, and must NOT make an *unbacked* one restorable. A row
    that asserts ``active`` with an empty approval — the state the new CHECK refuses, and the
    state a pre-migration row is in — comes back in shadow, exactly as it does today.
    """
    from merchant_svc.envelope.model import ACTIVE, SHADOW  # noqa: PLC0415
    from merchant_svc.envelope.repository import PostgresEnvelopeRepository  # noqa: PLC0415
    from merchant_svc.envelope.store import EnvelopeVersions  # noqa: PLC0415

    connection = _RoundTrip()
    connection.rows[("s-unbacked", 1)] = ("s-unbacked", 1, ACTIVE, 10.0, 100.0, "[]", "[]", "[]")

    versions = EnvelopeVersions(PostgresEnvelopeRepository(connection))
    assert versions.current("s-unbacked").activation == SHADOW
    assert versions.is_live("s-unbacked") is False
