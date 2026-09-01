"""Frozen acceptance suite — cross-cutting SPEC criteria + the three S8 release blockers.

FROZEN. Hashed by `swarmloop.py freeze`. Workers may run this file; nobody may edit it.

Epic coverage in this file:

* **epic SPEC** (8 tests) — criteria that no single epic owns: the suite's own two hygiene
  self-tests (README rules 1 and 2), the C1 monorepo layout, the C2/C7 compose declaration,
  both halves of C3/S7 (exchange import isolation + the Postgres grant model), the C11
  frozen `LedgerEvent` kind enum, and C8's no-timelines rule over the planning and demo docs.
* **epic E3** (3 tests) — the three SPEC S8 release blockers. They live in this file because
  `README.md` puts them here; they carry `epic("E3")` because their owning tickets (T-032,
  T-033) are exchange tickets and a green `e3_exchange_passing` must never be able to coexist
  with a red release blocker. File placement and epic marker are separate axes; `run.py`
  reads only markers.

Every test imports product code **inside** its function body (README rule 1), so an unbuilt
goal is a clean per-test failure rather than a collection error that silently shrinks the
denominator. Module scope holds stdlib + pytest only.
"""
from __future__ import annotations

import ast
import enum
import pathlib
import re
import typing
from urllib.parse import urlsplit

import pytest

# ---------------------------------------------------------------------------------
# Paths. `.swarm-loop/acceptance/` -> repo root is two parents up.
# ---------------------------------------------------------------------------------
ACCEPTANCE_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = ACCEPTANCE_DIR.parents[1]

# First dotted segments that mean "product code" for README rule 1.
PRODUCT_ROOTS = frozenset(
    {"apps", "packages", "services", "e2e", "fixtures", "pixel", "db", "graph", "evals"}
)

VALID_EPICS = frozenset({"E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "SPEC"})
TICKET_RE = re.compile(r"^T-\d{3}$")
BLOCKER_RE = re.compile(r"^S8-[123]$")

# Directory names never walked when scanning the source tree.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".next",
        ".turbo",
        ".codegraph",
        ".swarm-loop",
    }
)

TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".json",
        ".toml",
        ".cfg",
        ".ini",
        ".yml",
        ".yaml",
        ".sql",
        ".md",
        ".mdx",
        ".txt",
        ".sh",
        ".env",
        ".graphql",
        ".prisma",
        "",
    }
)

_MISSING = object()


# ---------------------------------------------------------------------------------
# Small, product-free helpers. These import nothing and assert nothing on their own.
# ---------------------------------------------------------------------------------
def _read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _iter_files(root: pathlib.Path, suffixes: frozenset[str] | None = None):
    """Yield every file under `root`, skipping vendored/cache directories."""
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:  # pragma: no cover - unreadable dir
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                if suffixes is None or entry.suffix in suffixes:
                    yield entry


def _acceptance_test_files() -> list[pathlib.Path]:
    return sorted(p for p in ACCEPTANCE_DIR.glob("test_*.py") if p.is_file())


def _function_nodes(tree: ast.AST):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _imports_inside_functions(tree: ast.AST) -> set[int]:
    """ids() of every Import/ImportFrom node that sits inside some function body."""
    inside: set[int] = set()
    for func in _function_nodes(tree):
        for node in ast.walk(func):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                inside.add(id(node))
    return inside


def _imported_roots(node: ast.AST) -> list[str]:
    """First dotted segment of every module an import statement names."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:  # relative import — never product code by dotted path
            return []
        if not node.module:
            return []
        return [node.module.split(".")[0]]
    return []


def _decorator_calls(func: ast.AST) -> list[tuple[str, list[object]]]:
    """[(marker_name, literal_args)] for decorators shaped `@pytest.mark.<name>(...)`."""
    found: list[tuple[str, list[object]]] = []
    for dec in getattr(func, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        target = dec.func
        if not isinstance(target, ast.Attribute):
            continue
        owner = target.value
        if not (isinstance(owner, ast.Attribute) and owner.attr == "mark"):
            continue
        args: list[object] = []
        for arg in dec.args:
            if isinstance(arg, ast.Constant):
                args.append(arg.value)
        found.append((target.attr, args))
    return found


def _get(obj: object, *names: str, default: object = _MISSING) -> object:
    """Read `names` off a mapping or an object, in order. Shape-agnostic on purpose:
    the assertion is about the value, never about dict-vs-dataclass."""
    for name in names:
        try:
            return obj[name]  # type: ignore[index]
        except Exception:
            pass
        value = getattr(obj, name, _MISSING)
        if value is not _MISSING:
            return value
    if default is not _MISSING:
        return default
    raise AssertionError(
        f"expected one of {names!r} on {type(obj).__name__} object {obj!r}"
    )


def _sequence(value: object) -> list:
    assert isinstance(value, (list, tuple)), f"expected a sequence, got {value!r}"
    return list(value)


def _strip_markdown_code(text: str) -> str:
    """Remove fenced blocks and inline code spans — identifiers and shell commands are
    never the "timeline prose" C8 forbids."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " ", text)
    return text


# C8 forbids schedule content. The vocabulary below is deliberately about *schedules*,
# not about the word "timeline" itself (SPEC.md's own C8 sentence contains it), and it
# carves out identifier-shaped and duration-shaped matches, which are units, not plans.
_TIMELINE_TERMS = (
    r"weeks?",
    r"weekly",
    r"sprints?",
    r"deadlines?",
    r"milestones?",
    r"months?",
    r"monthly",
    r"quarters?",
    r"days?",
    r"daily",
    r"overnight",
    r"eta",
    r"timeframes?",
    r"mondays?",
    r"tuesdays?",
    r"wednesdays?",
    r"thursdays?",
    r"fridays?",
    r"saturdays?",
    r"sundays?",
    r"january",
    r"february",
    r"april",
    r"june",
    r"july",
    r"august",
    r"september",
    r"october",
    r"november",
    r"december",
    r"q[1-4]\b",
    r"\d{4}-\d{2}-\d{2}",
)
_TIMELINE_RE = re.compile(
    r"(?<![\w-])(?:" + "|".join(_TIMELINE_TERMS) + r")(?![\w])", re.IGNORECASE
)


def _timeline_hits(text: str) -> list[str]:
    """Schedule-shaped prose in `text`. Code spans stripped; `60-day`, `deadline_at`,
    `respond_by` and friends are excluded by the identifier/duration guards."""
    body = _strip_markdown_code(text)
    hits: list[str] = []
    for match in _TIMELINE_RE.finditer(body):
        start, end = match.span()
        window = body[max(0, start - 40) : min(len(body), end + 40)].replace("\n", " ")
        hits.append(f"{match.group(0)!r} in ...{window.strip()}...")
    return hits


def _compose_path() -> pathlib.Path | None:
    for name in (
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ):
        candidate = REPO_ROOT / name
        if candidate.is_file():
            return candidate
    return None


def _compose_services(text: str) -> dict[str, str]:
    """Top-level `services:` children and the raw text of each block. Textual on purpose:
    the frozen suite may not assume PyYAML is installed (--confcutdir, no plugins)."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^services:\s*(#.*)?$", line):
            start = index + 1
            break
    if start is None:
        return {}

    child_indent = None
    services: dict[str, list[str]] = {}
    current = None
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            if current is not None:
                services[current].append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            break
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            match = re.match(r"^\s*([A-Za-z0-9._-]+)\s*:\s*(#.*)?$", line)
            current = match.group(1) if match else None
            if current is not None:
                services[current] = []
        elif current is not None:
            services[current].append(line)
    return {name: "\n".join(body) for name, body in services.items()}


def _compose_include_paths(text: str) -> list[pathlib.Path]:
    """Fragment paths listed under a top-level ``include:`` block, resolved from the root.

    Textual for the same reason as :func:`_compose_services`: the frozen suite may not
    assume PyYAML is installed. Reads the short form the root file uses (``- a/b.yaml``)
    and the ``- path: a/b.yaml`` long form.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^include:\s*(#.*)?$", line):
            start = index + 1
            break
    if start is None:
        return []

    paths: list[pathlib.Path] = []
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip(" ")) == 0:
            break
        match = re.match(r"^\s*-\s*(?:path:\s*)?(\S+)\s*$", line)
        if match:
            candidate = REPO_ROOT / match.group(1)
            if candidate.is_file():
                paths.append(candidate)
    return paths


def _compose_stack_services() -> dict[str, str]:
    """Every service the compose stack declares: the root file's own ``services:`` block
    plus the block of every fragment its ``include:`` list pulls in.

    AMENDMENT 2. The four deployables (buyer/merchant/exchange/trust) live in per-lane
    fragments *by design* — the root file's own header states that a service ticket fills
    its own fragment and that no ticket ever edits the root file — so a check that read
    only the root could never see them, no matter what any ticket built. Reading the
    fragments makes C1 reachable; it does not make it free, because a fragment that is
    still an empty ``services: {}`` stub contributes nothing.
    """
    path = _compose_path()
    if path is None:
        return {}
    text = _read_text(path)
    services = _compose_services(text)
    for fragment in _compose_include_paths(text):
        for name, block in _compose_services(_read_text(fragment)).items():
            services.setdefault(name, block)
    return services


def _sql_statements(text: str) -> list[str]:
    """Whitespace-normalised, comment-stripped SQL statements."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    return [re.sub(r"\s+", " ", stmt).strip() for stmt in text.split(";") if stmt.strip()]


_EXCHANGE_ROLE_RE = re.compile(r"\bexchange[a-z0-9_]*\b")


def _ledger_event_kinds(contracts_module: object) -> set[str]:
    """The kind vocabulary `packages.contracts` publishes for `LedgerEvent`.

    Preferred spelling is a `LedgerEventKind` enum; a `Literal`/enum annotation on
    `LedgerEvent.kind` is accepted too. Where the enum *lives* is not the criterion —
    C11 is about which kinds exist — so both spellings are read, and neither weakens
    the exact-set assertion below.
    """
    holder = getattr(contracts_module, "LedgerEventKind", None)
    if holder is None:
        holder = getattr(contracts_module, "LedgerEventKindEnum", None)
    if holder is not None:
        return _kinds_from_annotation(holder)

    event = getattr(contracts_module, "LedgerEvent", None)
    assert event is not None, (
        "packages.contracts must export LedgerEvent (and, preferably, LedgerEventKind) "
        "so the C11 frozen event vocabulary is readable"
    )
    fields = getattr(event, "model_fields", None) or getattr(event, "__fields__", None)
    annotation = None
    if fields is not None and "kind" in fields:
        annotation = getattr(fields["kind"], "annotation", None) or getattr(
            fields["kind"], "type_", None
        )
    if annotation is None:
        annotation = typing.get_type_hints(event).get("kind")
    assert annotation is not None, (
        "could not read the `kind` vocabulary from packages.contracts.LedgerEvent"
    )
    return _kinds_from_annotation(annotation)


def _kinds_from_annotation(annotation: object) -> set[str]:
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return {str(member.value) for member in annotation}
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return {str(arg) for arg in typing.get_args(annotation)}
    if isinstance(annotation, (list, tuple, set, frozenset)):
        return {str(item) for item in annotation}
    raise AssertionError(
        f"cannot read a kind vocabulary out of {annotation!r}; expected an Enum, a "
        f"typing.Literal, or a collection of kind strings"
    )


# ---------------------------------------------------------------------------------
# Deterministic ranking / accept fixtures for the three S8 release blockers.
#
# These builders are deliberately IDENTICAL IN SHAPE to `test_e3_exchange.py`'s: the same
# candidate record (hard-constraint evidence carried by `claims[i]["status"]`, the seller's
# registered domain at `store_domain`, a float epoch `expires_at`), the same trust-snapshot
# row, the same `config={"now": ...}`, the same auction, and the same result shape
# (`result["shortlist"]["slots"]`, `result["candidates"]`, `result["ranked"]`).
#
# `rank()` and `accept()` are owned by T-032 / T-033 and are read by BOTH files, so there is
# exactly ONE published contract for them. Any divergence here would pin these three release
# blockers red for an implementation that satisfies the 15 E3 tests — which is why the shapes
# below are the E3 file's, verbatim. No clock, no randomness, no I/O.
# ---------------------------------------------------------------------------------
T_NOW = 1_700_000_000.0
T_PAST = 1_600_000_000.0
T_FUTURE = 2_000_000_000.0

# The single hard constraint these blockers exercise, in `test_e3_exchange.py`'s spelling.
HARD_FIELD = "capacity_l"
HARD_MIN = 30
HARD_CLAIMED = 35  # satisfies `capacity_l >= 30` on VALUE, so only `status` can decide

SELLER_DOMAIN = "store-a.example.com"
RIVAL_DOMAIN = "rival.example.com"


def _claim(key, value, source: str = "owner_statement", status: str = "verified") -> dict:
    """A supporting fact in the E3 shape. `status` is the only evidence R19 may read: a
    hard constraint is satisfied by a `verified` supporting claim and by nothing else."""
    return {
        "key": key,
        "value": value,
        "provenance": {
            "source": source,
            "ref": f"ref:{key}",
            "observed_at": T_PAST,
            "authority_rank": 1,
        },
        "status": status,
    }


def _intent() -> dict:
    return {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "query": "a 30 litre commuter backpack",
        "category": "backpacks",
        "hard_constraints": [{"field": HARD_FIELD, "op": "gte", "value": HARD_MIN}],
        "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
        "ship_to": "US",
        "currency": "USD",
        "budget_band": "100-200",
        "created_at": T_PAST,
        "schema_version": "1.0.0",
    }


def _config() -> dict:
    """Ranker config. `now` is passed in, so no blocker ever reads the wall clock."""
    return {"now": T_NOW}


def _offer(price: float, domain: str, *, checkout_url: str | None = None) -> dict:
    if checkout_url is None:
        checkout_url = f"https://{domain}/cart/1:1?discount=NET"
    return {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "checkout_url": checkout_url,
        "expires_at": T_FUTURE,
    }


def _candidate(
    bid_id: str,
    store_id: str,
    *,
    strength: float,
    hard_status: str = "verified",
    price: float = 50.0,
    network_fee: float = 0.0,
    fee_rate: float = 0.0,
    tier: int = 1,
) -> dict:
    """One ranking candidate. `strength` drives every published scoring feature at once, so
    a "maximal" candidate is maximal on every term of the DESIGN rank formula. The hard
    constraint is evidenced only by `claims`, whose value always satisfies `capacity_l >= 30`
    — `hard_status` is therefore the sole difference between an honest and a lying bid."""
    domain = f"{store_id}.example.com"
    return {
        "bid_id": bid_id,
        "store_id": store_id,
        "store_domain": domain,
        "tier": tier,
        "network_fee": network_fee,
        "fee_rate": fee_rate,
        "envelope_max_discount_pct": 0.0,
        "envelope_budget_cap": 0.0,
        "offer": _offer(price, domain),
        "claims": [
            _claim(HARD_FIELD, HARD_CLAIMED, status=hard_status),
            _claim("ships_in_days", 2),
        ],
        "intent_match": strength,
        "price_value": strength,
        "delivery_fit": strength,
        "verified_claim_ratio": 1.0,
        "policy_penalties": 0.0,
    }


def _snapshot(
    store_id: str, *, blacklisted: bool = False, score: float = 0.8, low_data: bool = False
) -> dict:
    """One store's TrustSnapshot row (DESIGN §Interfaces / decision D26), in the E3 shape.
    The snapshot is a mapping keyed by `store_id`; `blacklisted` lives on the row."""
    dims = {
        name: {"alpha": 8.0, "beta": 2.0, "decayed_at": T_PAST}
        for name in (
            "price_honored",
            "discount_honored",
            "shipped_on_time",
            "not_returned",
            "feedback_match",
        )
    }
    return {
        "store_id": store_id,
        "score": float(score),
        "confidence": 0.9,
        "dims": dims,
        "blacklisted": blacklisted,
        "low_data": low_data,
    }


def _slots(result: object) -> list:
    """R2/A6: the shortlist is published at `result["shortlist"]["slots"]` — one location,
    shared with `test_e3_exchange.py`."""
    return _sequence(_get(_get(result, "shortlist"), "slots"))


def _slot_refs(result: object) -> list[str]:
    return [str(_get(slot, "bid_ref", "bid_id")) for slot in _slots(result)]


def _candidate_record(result: object, bid_id: str) -> object:
    """The per-candidate eligibility record T-032 acceptance 3 requires the ranker to
    record (`ranking_candidates(eligible, components, exclusion_reasons)`)."""
    records = _sequence(_get(result, "candidates", "ranked_candidates", "candidate_records"))
    for record in records:
        if str(_get(record, "bid_id", "bid_ref", default="")) == bid_id:
            return record
    raise AssertionError(
        f"rank() recorded no candidate row for {bid_id!r}; T-032 must record eligibility "
        f"and exclusion_reasons for every candidate it was given"
    )


def _assert_excluded(result: object, bid_id: str, reason_substrings: tuple[str, ...]) -> None:
    assert bid_id not in _slot_refs(result), (
        f"{bid_id!r} must never occupy a shortlist slot; slots were {_slot_refs(result)!r}"
    )
    record = _candidate_record(result, bid_id)
    eligible = _get(record, "eligible", default=_MISSING)
    assert eligible is not _MISSING, f"candidate row for {bid_id!r} records no `eligible` flag"
    assert not eligible, f"{bid_id!r} must be recorded ineligible, got eligible={eligible!r}"
    reasons = _sequence(_get(record, "exclusion_reasons", default=[]))
    blob = " ".join(str(reason) for reason in reasons).lower()
    assert reasons, f"{bid_id!r} was excluded with no exclusion_reasons recorded"
    assert any(token in blob for token in reason_substrings), (
        f"exclusion_reasons for {bid_id!r} must name the filter that fired "
        f"(one of {reason_substrings!r}); got {reasons!r}"
    )


class _RecordingCodeCreator:
    """In-process stand-in for merchant `POST /codes {store_id, offer} -> {code,
    permalink_url}`. Records every call so a refused accept can be shown to have created
    no code. Callable and method-shaped, so either call style works.

    It ECHOES the offer's checkout URL on purpose: a creator that manufactured its own
    on-domain permalink would make the off-domain blocker unfalsifiable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def create_code(self, store_id, offer, *args, **kwargs):
        self.calls.append((str(store_id), dict(offer) if isinstance(offer, dict) else offer))
        url = _get(offer, "checkout_url", default=f"https://{SELLER_DOMAIN}/cart/1:1")
        return {"code": "PSX-ABCDEFGH", "permalink_url": str(url)}

    __call__ = create_code


def _auction(checkout_domain: str = SELLER_DOMAIN, *, bid_id: str = "bid-a") -> dict:
    """A one-bid auction in the E3 shape. The seller's REGISTERED domain is `bid["store_domain"]`
    — the single published location D22's exact-host comparison reads. `checkout_domain` only
    decides where the offer's checkout URL points, so a hostile URL can be built without ever
    changing what the seller is registered as."""
    return {
        "auction_id": "auction-1",
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "bids": [
            {
                "bid_id": bid_id,
                "store_id": "store-a",
                "store_domain": SELLER_DOMAIN,
                "offer": _offer(50.0, checkout_domain),
            }
        ],
        "accepted_bid_ref": None,
        "now": T_NOW,
    }


def _permalink_of(result: object) -> object:
    return _get(result, "permalink_url", "permalink", default=None)


# ---------------------------------------------------------------------------------
# 1-2. Suite hygiene. These assert properties of the FROZEN SUITE, not of the product,
# so they are green from the moment the suite is written correctly — and they go red the
# instant someone breaks README rule 1 or rule 2, which is the only defence that turns an
# invisible denominator shrink into a visible failing test.
# ---------------------------------------------------------------------------------
@pytest.mark.epic("SPEC")
@pytest.mark.ticket("T-000")
def test_acceptance_suite_has_no_module_scope_product_imports():
    """README rule 1: no acceptance test file imports product code at module scope."""
    files = _acceptance_test_files()
    assert files, f"no test_*.py files found in {ACCEPTANCE_DIR} — the suite is empty"

    violations: list[str] = []
    for path in files:
        tree = ast.parse(_read_text(path), filename=str(path))
        nested = _imports_inside_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if id(node) in nested:
                continue
            for root in _imported_roots(node):
                if root in PRODUCT_ROOTS:
                    violations.append(f"{path.name}:{node.lineno}: module-scope import of {root!r}")

    assert not violations, (
        "module-scope product imports turn an unmet goal into a collection error and "
        "silently shrink the acceptance denominator:\n  " + "\n  ".join(violations)
    )


@pytest.mark.epic("SPEC")
@pytest.mark.ticket("T-000")
def test_every_acceptance_test_is_marked_with_epic_and_ticket():
    """README rule 2: every acceptance test carries a valid epic marker and ticket marker."""
    files = _acceptance_test_files()
    assert files, f"no test_*.py files found in {ACCEPTANCE_DIR} — the suite is empty"

    problems: list[str] = []
    total = 0
    for path in files:
        tree = ast.parse(_read_text(path), filename=str(path))
        for func in _function_nodes(tree):
            if not func.name.startswith("test_"):
                continue
            total += 1
            where = f"{path.name}::{func.name}"
            markers = dict(_decorator_calls(func))
            if "epic" not in markers:
                problems.append(f"{where}: no @pytest.mark.epic(...)")
            elif not markers["epic"] or str(markers["epic"][0]) not in VALID_EPICS:
                problems.append(f"{where}: epic {markers['epic']!r} not in {sorted(VALID_EPICS)}")
            if "ticket" not in markers:
                problems.append(f"{where}: no @pytest.mark.ticket(...)")
            elif not markers["ticket"] or not TICKET_RE.match(str(markers["ticket"][0])):
                problems.append(f"{where}: ticket {markers['ticket']!r} is not T-nnn shaped")
            if "blocker" in markers and (
                not markers["blocker"] or not BLOCKER_RE.match(str(markers["blocker"][0]))
            ):
                problems.append(f"{where}: blocker {markers['blocker']!r} is not S8-n shaped")

    assert total, "the acceptance suite defines no test functions at all"
    assert not problems, (
        "an unmarked acceptance test counts toward no metric and is a defect in the "
        "suite:\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------------------------
# 3-4. C1 / C2 / C7 — the shape of the repo and of its local runtime.
# ---------------------------------------------------------------------------------
@pytest.mark.epic("SPEC")
@pytest.mark.ticket("T-000")
def test_monorepo_layout_matches_design_and_has_no_packages_protocol():
    """C1 (+ decision D1): the DESIGN workspace layout exists and `packages/protocol` does not."""
    required = [
        "apps/buyer",
        "apps/exchange",
        "apps/trust",
        "apps/merchant",
        "apps/seller-reference",
        "packages/contracts",
        "packages/store-agent",
        "packages/verification",
        "packages/llm",
        "services/ingest",
        "services/shopify-stub",
        "services/sim",
        "pixel",
        "db/migrations",
        "e2e",
        "fixtures",
    ]
    missing = [rel for rel in required if not (REPO_ROOT / rel).is_dir()]
    assert not missing, f"DESIGN workspace directories missing from the monorepo: {missing}"

    assert not (REPO_ROOT / "packages" / "protocol").exists(), (
        "packages/protocol must not exist — packages/contracts is the schema home (D1)"
    )

    source_roots = ["apps", "packages", "services", "pixel", "db", "e2e", "fixtures", "graph", "evals"]
    offenders: list[str] = []
    for name in source_roots:
        for path in _iter_files(REPO_ROOT / name, TEXT_SUFFIXES):
            text = _read_text(path)
            if "packages/protocol" in text or "packages.protocol" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "the string `packages/protocol` must not appear anywhere in the source tree "
        f"(D1); found in: {sorted(offenders)}"
    )


@pytest.mark.epic("SPEC")
@pytest.mark.ticket("T-000")
def test_compose_declares_healthchecked_postgres_neo4j_redis_and_separate_deployables():
    """C2/C7/C1: compose declares healthchecked postgres+neo4j+redis and one service per deployable."""
    path = _compose_path()
    assert path is not None, (
        "no docker-compose file at the repo root — C7 requires every service to run under "
        "docker-compose locally"
    )
    services = _compose_stack_services()
    assert services, f"{path.name} declares no top-level `services:` block"

    def _find(token: str) -> str | None:
        for name, block in services.items():
            if token in name.lower() or re.search(
                rf"^\s*image:\s*\S*{token}", block, re.IGNORECASE | re.MULTILINE
            ):
                return name
        return None

    for token in ("postgres", "neo4j", "redis"):
        name = _find(token)
        assert name is not None, (
            f"compose declares no {token} service (C2); services are {sorted(services)}"
        )
        assert re.search(r"^\s*healthcheck:", services[name], re.MULTILINE), (
            f"compose service {name!r} ({token}) declares no healthcheck — T-000 acceptance 2 "
            f"requires postgres+neo4j+redis brought up healthchecked"
        )

    deployables = {}
    for token in ("buyer", "merchant", "exchange", "trust"):
        matches = [name for name in services if token in name.lower()]
        assert matches, (
            f"compose declares no service for the {token!r} deployable (C1: buyer/merchant/"
            f"exchange/trust are separate deployables); services are {sorted(services)}"
        )
        deployables[token] = matches[0]
    assert len(set(deployables.values())) == 4, (
        f"buyer/merchant/exchange/trust must be four distinct compose services, got {deployables}"
    )


# ---------------------------------------------------------------------------------
# 5-6. C3 / S7 — both halves of "the exchange cannot read envelopes", proved statically.
# ---------------------------------------------------------------------------------
@pytest.mark.epic("SPEC")
@pytest.mark.ticket("T-011")
def test_exchange_source_never_imports_envelope_or_sealed_surfaces():
    """C3/S7 (import half): no code under apps/exchange reaches an envelope or sealed surface."""
    exchange = REPO_ROOT / "apps" / "exchange"
    assert exchange.is_dir(), "apps/exchange does not exist — C3 has nothing to protect yet"
    python_files = list(_iter_files(exchange, frozenset({".py"})))
    assert python_files, "apps/exchange contains no Python modules — C3 has nothing to protect yet"

    violations: list[str] = []
    literal_re = re.compile(r"sealed\.|envelopes", re.IGNORECASE)
    for path in python_files:
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(_read_text(path), filename=str(path))
        for node in ast.walk(tree):
            dotted: list[str] = []
            if isinstance(node, ast.Import):
                dotted = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                dotted = [base] + [f"{base}.{alias.name}" for alias in node.names]
            for name in dotted:
                low = name.lower()
                if "envelope" in low or "sealed" in low:
                    violations.append(f"{rel}:{node.lineno}: imports {name!r}")

        # Raw-SQL and table-name reach-throughs are code paths an import scan cannot see.
        # Test modules are excluded from the literal scan only: a test that *names* the
        # forbidden surface in prose is not a code path, while an import of it still is.
        parts = set(path.relative_to(REPO_ROOT).parts)
        is_test = "tests" in parts or path.name.startswith("test_") or path.name.endswith("_test.py")
        if is_test:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if literal_re.search(node.value):
                    violations.append(
                        f"{rel}:{node.lineno}: string literal reaches a sealed/envelope "
                        f"surface: {node.value[:80]!r}"
                    )

    assert not violations, (
        "C3/S7: the exchange must have no code path that can read envelopes:\n  "
        + "\n  ".join(sorted(violations))
    )


@pytest.mark.epic("SPEC")
@pytest.mark.ticket("T-011")
def test_migrations_never_grant_exchange_access_to_sealed_or_vault():
    """C3/S7 (grant half): the exchange role holds USAGE on ledger and app only (D5)."""
    migrations = REPO_ROOT / "db" / "migrations"
    assert migrations.is_dir(), "db/migrations does not exist"
    sql_files = sorted(_iter_files(migrations, frozenset({".sql"})))
    assert sql_files, "db/migrations contains no .sql files — the grant model is unproven"

    violations: list[str] = []
    granted_schemas: set[str] = set()
    for path in sql_files:
        rel = str(path.relative_to(REPO_ROOT))
        for stmt in _sql_statements(_read_text(path)):
            low = stmt.lower()
            if "grant" not in low or not _EXCHANGE_ROLE_RE.search(low):
                continue
            if "sealed" in low or "vault" in low:
                violations.append(f"{rel}: grant reaches sealed/vault for the exchange role: {stmt[:160]!r}")
            if " on " not in f" {low} ":
                grantee = low.split(" to ", 1)[1] if " to " in low else ""
                if _EXCHANGE_ROLE_RE.search(grantee):
                    violations.append(
                        f"{rel}: role-membership grant to the exchange role bypasses the "
                        f"schema-USAGE model (D5): {stmt[:160]!r}"
                    )
            match = re.search(r"\bon\s+schema\s+([a-z0-9_,\s\"]+?)\s+to\s", low)
            if match:
                for schema in match.group(1).split(","):
                    schema = schema.strip().strip('"')
                    if schema:
                        granted_schemas.add(schema)

    assert not violations, "C3/S7 grant violations:\n  " + "\n  ".join(violations)
    assert granted_schemas == {"ledger", "app"}, (
        "D5: the exchange role is granted USAGE on `ledger` and `app` only, never on "
        f"`sealed` or `vault`; migrations grant it schemas {sorted(granted_schemas)}"
    )


# ---------------------------------------------------------------------------------
# 7. C11 — the frozen event vocabulary.
# ---------------------------------------------------------------------------------
@pytest.mark.epic("SPEC")
@pytest.mark.ticket("T-010")
def test_ledger_event_kind_enum_is_exactly_the_frozen_set():
    """C11: LedgerEvent.kind is exactly the frozen vocabulary — no more, no fewer."""
    import packages.contracts as contracts

    expected = {
        # DESIGN §Interfaces LedgerEvent enum
        "bid_placed",
        "shown",
        "accepted",
        "code_created",
        "checkout_redirect",
        "checkout_pixel",
        "order_paid",
        "order_fulfilled",
        "refund",
        "feedback",
        "reconciled",
        "claim_verified",
        "policy_event",
        # extended exactly once, in T-010, per pinned decision D24
        "auction_opened",
        "auction_closed",
        "offer_integrity",
        "blacklisted",
        "blacklist_expired",
    }
    actual = _ledger_event_kinds(contracts)
    assert actual == expected, (
        "C11 freezes the LedgerEvent kind vocabulary jointly across the redirect and "
        f"Shopify checkout paths.\n  missing: {sorted(expected - actual)}\n  "
        f"unexpected: {sorted(actual - expected)}"
    )


# ---------------------------------------------------------------------------------
# 8. C8 — no timelines in the planning or demo docs.
# ---------------------------------------------------------------------------------
@pytest.mark.epic("SPEC")
@pytest.mark.ticket("T-085")
def test_planning_and_demo_docs_contain_no_timeline_language():
    """C8: no schedule prose in SPEC/DESIGN/TASKS/EXECUTION or in docs/."""
    docs_dir = REPO_ROOT / "docs"
    assert docs_dir.is_dir(), "docs/ does not exist — the demo runbook (S6) has no home"

    targets = [REPO_ROOT / name for name in ("SPEC.md", "DESIGN.md", "TASKS.md", "EXECUTION.md")]
    missing = [p.name for p in targets if not p.is_file()]
    assert not missing, f"planning documents missing: {missing}"

    doc_suffixes = frozenset({".md", ".mdx", ".rst", ".txt"})
    doc_files = [
        path
        for path in _iter_files(docs_dir, doc_suffixes)
        if "tests" not in path.relative_to(REPO_ROOT).parts
    ]
    assert doc_files, "docs/ contains no documentation files"
    targets.extend(sorted(doc_files))

    findings: list[str] = []
    for path in targets:
        for hit in _timeline_hits(_read_text(path)):
            findings.append(f"{path.relative_to(REPO_ROOT)}: {hit}")
    assert not findings, (
        "C8 forbids timelines in these documents; identifier and duration forms "
        "(`deadline_at`, `respond_by`, `60-day window`, fenced code) are already "
        "carved out:\n  " + "\n  ".join(findings)
    )


# ---------------------------------------------------------------------------------
# 9-11. SPEC S8 release blockers. Marked epic E3 — their owning tickets are exchange
# tickets, and `e3_exchange_passing` must never be able to read "done" while one of these
# is red. `--blocker S8-n` selects them independently of epic.
#
# `rank()` and `accept()` are called here through EXACTLY the surface `test_e3_exchange.py`
# calls them through — positional arguments, E3-shaped inputs, `result["shortlist"]["slots"]`
# — so one implementation satisfies both files. See the fixture header above.
# ---------------------------------------------------------------------------------
@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
@pytest.mark.blocker("S8-1")
def test_blacklisted_seller_is_never_eligible():
    """S8 blocker 1 (R12): a blacklisted seller never surfaces as eligible, and an
    unavailable blacklist read fails closed."""
    from apps.exchange.src.ranking import rank

    intent = _intent()

    # A blacklisted store is excluded at every combination of the published features,
    # including when it dominates a clean rival on all of them.
    for bad_strength in (0.0, 0.5, 1.0):
        for good_strength in (0.0, 0.5, 1.0):
            good = _candidate("bid-good", "store-good", strength=good_strength)
            bad = _candidate("bid-bad", "store-bad", strength=bad_strength)
            snapshot = {
                "store-good": _snapshot("store-good"),
                "store-bad": _snapshot("store-bad", blacklisted=True, score=1.0),
            }
            result = rank([good, bad], intent, snapshot, _config())
            _assert_excluded(result, "bid-bad", ("blacklist", "black_list", "blocked"))
            assert "bid-good" in _slot_refs(result), (
                "the clean candidate must still be shortlisted — a ranker that shortlists "
                "nobody satisfies the blocker vacuously"
            )

    # Sole candidate: the shortlist collapses to empty rather than admitting the blacklisted store.
    lone = _candidate("bid-bad", "store-bad", strength=1.0)
    result = rank(
        [lone],
        intent,
        {"store-bad": _snapshot("store-bad", blacklisted=True, score=1.0)},
        _config(),
    )
    assert _slot_refs(result) == [], (
        "a blacklisted store must not be shortlisted even when it is the only candidate"
    )

    # Fail-closed (R12): an unavailable blacklist read excludes rather than admits.
    unknown = _candidate("bid-unknown", "store-unknown", strength=1.0)
    good = _candidate("bid-good", "store-good", strength=0.1)
    for broken_snapshot in (
        {"store-good": _snapshot("store-good")},  # no row at all for store-unknown
        {
            "store-good": _snapshot("store-good"),
            "store-unknown": {**_snapshot("store-unknown"), "blacklisted": None},
        },
    ):
        result = rank([good, unknown], intent, broken_snapshot, _config())
        assert "bid-unknown" not in _slot_refs(result), (
            "R12 requires blacklist reads to fail closed: a store whose blacklist status "
            "cannot be read must be excluded, never admitted"
        )
        assert "bid-good" in _slot_refs(result), (
            "failing closed on one unreadable store must not empty the shortlist — a ranker "
            "that shortlists nobody satisfies the blocker vacuously"
        )


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
@pytest.mark.blocker("S8-2")
def test_contradicted_hard_constraint_never_wins():
    """S8 blocker 2 (R19): a contradicted hard constraint never wins, whatever the pitch quality."""
    from apps.exchange.src.ranking import rank

    intent = _intent()
    snapshot = {
        "store-liar": _snapshot("store-liar", score=1.0),
        "store-honest": _snapshot("store-honest", score=0.2),
    }

    # Both bids CLAIM a satisfying value (capacity_l = 35 >= 30); only the evidence status
    # differs. The liar is maximal on every published rank feature and cheapest; the honest
    # rival is minimal on every feature and the most expensive. Nothing but R19 can order them.
    liar = _candidate("bid-liar", "store-liar", strength=1.0, hard_status="contradicted", price=1.0)
    honest = _candidate("bid-honest", "store-honest", strength=0.0, hard_status="verified", price=999.0)

    result = rank([liar, honest], intent, snapshot, _config())
    _assert_excluded(result, "bid-liar", ("hard", "constraint", "contradict"))
    assert "bid-honest" in _slot_refs(result), (
        "the candidate whose hard constraint is verified must be shortlisted — otherwise "
        "the blocker is satisfied by a ranker that shortlists nobody"
    )
    assert _slot_refs(result)[0] == "bid-honest", (
        "R19: a contradicted hard constraint cannot win regardless of pitch quality"
    )

    # Even as the only candidate, it never occupies a slot.
    result = rank(
        [liar], intent, {"store-liar": _snapshot("store-liar", score=1.0)}, _config()
    )
    assert _slot_refs(result) == [], (
        "a candidate contradicting a hard constraint must not be shortlisted even when it "
        "is the only candidate"
    )


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-033")
@pytest.mark.blocker("S8-3")
def test_offdomain_checkout_url_is_refused():
    """S8 blocker 3 (C10, D22): no checkout URL outside the registered seller domain is returned."""
    from apps.exchange.src.accept import accept

    # `CHECKOUT_MODE` is pinned to {redirect, shopify_stub} by decision D23; "shopify" is
    # the spelling used elsewhere in the doc set. Every spelling must refuse the off-domain
    # offer, and at least one must actually complete an on-domain accept — that positive
    # control is what stops a stub that refuses everything from satisfying this blocker.
    modes = ("redirect", "shopify_stub", "shopify")

    working_modes: list[str] = []
    for mode in modes:
        creator = _RecordingCodeCreator()
        try:
            result = accept(_auction(SELLER_DOMAIN), "bid-a", creator, mode)
        except Exception:
            continue
        permalink = _permalink_of(result)
        if permalink:
            assert urlsplit(str(permalink)).hostname == SELLER_DOMAIN, (
                f"mode={mode!r}: accept returned a permalink outside the registered seller "
                f"domain {SELLER_DOMAIN!r}: {permalink!r}"
            )
            working_modes.append(mode)
    assert working_modes, (
        "accept() completed no on-domain accept in any of the checkout modes "
        f"{modes!r} — the blocker cannot be evidenced by a surface that refuses everything"
    )

    # Every hostile host below is rejected by an exact comparison against the registered
    # domain and by nothing weaker: the subdomain defeats `endswith`, the two spoofs defeat
    # `in`/substring matching, and the userinfo trick defeats `startswith` on the raw URL.
    hostile = {
        "plain other domain": f"https://{RIVAL_DOMAIN}/cart/1:1?discount=PSX-ABCDEFGH",
        "suffix spoof": f"https://evil-{SELLER_DOMAIN}.attacker.tld/cart/1:1?discount=PSX-ABCDEFGH",
        "glued suffix spoof": f"https://evil-{SELLER_DOMAIN}/cart/1:1?discount=PSX-ABCDEFGH",
        "userinfo spoof": f"https://{SELLER_DOMAIN}@attacker.tld/cart/1:1?discount=PSX-ABCDEFGH",
        "subdomain": f"https://checkout.{SELLER_DOMAIN}/cart/1:1?discount=PSX-ABCDEFGH",
    }
    for mode in modes:
        for label, url in hostile.items():
            auction = _auction(SELLER_DOMAIN)
            auction["bids"][0]["offer"]["checkout_url"] = url
            creator = _RecordingCodeCreator()
            try:
                result = accept(auction, "bid-a", creator, mode)
            except Exception:
                assert not creator.calls, (
                    f"mode={mode!r} / {label}: accept refused but had already created a "
                    f"discount code for an off-domain checkout URL"
                )
                continue
            permalink = _permalink_of(result)
            assert not permalink, (
                f"mode={mode!r} / {label}: accept returned permalink {permalink!r} for a "
                f"checkout URL outside the registered seller domain {SELLER_DOMAIN!r} "
                f"({url}). D22 compares the permalink host to the registered domain "
                f"`bid['store_domain']` by exact match — no subdomain wildcards, no "
                f"suffix matching, no userinfo tricks."
            )
            assert not creator.calls, (
                f"mode={mode!r} / {label}: the checkout domain must be validated before a "
                f"single-use discount code is created"
            )
