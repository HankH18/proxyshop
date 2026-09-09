"""Gates on ``scripts/collect_real_catalogs.py`` — the roster, and the resume.

This is the one script in the repository that talks to other companies' servers, so the two
things added here are the two things that decide how often it does that:

**The roster** used to be a tuple in the source and ``--only`` used to be filtered against that
tuple *and nothing else*. ``--only allbirds.com`` therefore produced an empty roster, fetched
nothing and exited 0 — a result no reader could tell apart from a roster that was empty on
purpose. Now the roster comes from a file that carries a **category** beside each host (breadth
is the point; a corpus that cannot report its categories cannot be checked for having them),
and every route to an empty roster is a hard error.

**The resume** exists because ``fetchlog.json`` used to be written once, after the last store.
A crash at store 900 threw away hours of deliberately slow fetching AND re-hit 900 merchants on
the retry. The danger in fixing that is worse than the bug: a half-collected store silently
treated as complete puts a truncated catalogue into the corpus wearing a whole catalogue's
label. So most of the resume tests below are about the ways "already collected" must FAIL —
retryable ending, changed settings, missing page, page whose bytes no longer match its digest.

**Nothing here opens a socket.** The network is an ``httpx.MockTransport``, which is a function
this file wrote; ``test_the_whole_fetch_build_round_trip_opens_no_socket`` proves it with
``socket.socket`` monkeypatched to explode rather than asserting it. Collection stays a
by-hand operation (D3/C9).
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "fixtures" / "real-catalogs"
INCUMBENT_HOSTS = CORPUS / "incumbent-hosts.txt"
CANDIDATE_HOSTS = CORPUS / "candidate-hosts.txt"


def _collector() -> Any:
    """The collector module, loaded from its path.

    By path rather than ``import scripts.collect_real_catalogs``: ``scripts/`` carries no
    ``__init__.py``, so importing it by name would depend on namespace-package resolution that
    a differently-invoked pytest might not give it. The path is a fact about the tree; the
    module name is a fact about the configuration.
    """
    path = REPO_ROOT / "scripts" / "collect_real_catalogs.py"
    spec = importlib.util.spec_from_file_location("_real_catalog_collector", path)
    assert spec is not None and spec.loader is not None, f"{path} is not importable"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


crc = _collector()


# ======================================================================================
# a fake storefront: enough Shopify to walk, and it counts every request
# ======================================================================================


class Storefront:
    """One host's canned responses, plus a tally of what was actually asked of it."""

    def __init__(
        self,
        *,
        products: int = 3,
        robots: str = "User-agent: *\nAllow: /\n",
        robots_status: int = 200,
        page_status: dict[int, int] | None = None,
        page_body: dict[int, bytes] | None = None,
    ) -> None:
        self.products = products
        self.robots = robots
        self.robots_status = robots_status
        self.page_status = page_status or {}
        self.page_body = page_body or {}
        self.requests: list[str] = []


class FakeInternet:
    """An ``httpx.MockTransport`` handler over a dict of :class:`Storefront`."""

    def __init__(self, hosts: dict[str, Storefront], page_size: int = 250) -> None:
        self.hosts = hosts
        self.page_size = page_size
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        host = request.url.host.removeprefix("www.")
        store = self.hosts.get(host)
        if store is None:  # a host nobody canned is a host that does not exist
            return httpx.Response(404, text="no such host")
        store.requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(store.robots_status, text=store.robots)
        page = int(request.url.params.get("page", "1"))
        status = store.page_status.get(page, 200)
        if status != 200:
            return httpx.Response(status, text="")
        if page in store.page_body:
            return httpx.Response(200, content=store.page_body[page])
        first = (page - 1) * self.page_size
        remaining = max(0, store.products - first)
        count = min(self.page_size, remaining)
        payload = {
            "products": [
                {
                    "id": 1_000_000 + first + n,
                    "handle": f"{host.split('.')[0]}-{first + n}",
                    "title": f"Product {first + n}",
                    "product_type": "Widget",
                    "variants": [{"id": 9_000 + first + n, "price": "12.00"}],
                }
                for n in range(count)
            ]
        }
        # Separators matter: `build` refuses a pretty-printed page because a JSONL line cannot
        # hold a verbatim record that contains newlines.
        return httpx.Response(200, content=json.dumps(payload, separators=(",", ":")).encode())


# Captured ONCE, at import, and this is not a detail. A `real_client = httpx.Client` read
# inside `install` reads whatever the PREVIOUS install left there, so the second fake internet
# in a test would wrap the first one and the first one's transport would win. Every "a resumed
# run made no requests" assertion would then pass because the tally it read was the wrong
# object — a green that measured nothing, which is the failure this whole file is about.
_REAL_HTTPX_CLIENT = httpx.Client


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeInternet]:
    """Install a fake internet and return the tally object. Callable more than once per test."""

    def install(hosts: dict[str, Storefront], page_size: int = 250) -> FakeInternet:
        fake = FakeInternet(hosts, page_size=page_size)

        def client(**kwargs: Any) -> httpx.Client:
            kwargs["transport"] = httpx.MockTransport(fake.handler)
            return _REAL_HTTPX_CLIENT(**kwargs)

        monkeypatch.setattr(httpx, "Client", client)
        return fake

    return install


@pytest.fixture(autouse=True)
def free_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the polite wait instantaneous — WITHOUT making the interval short.

    Tests used to pass ``--min-interval 0``, which is now a hard error: the 2.0-second gap is a
    floor rather than a default, enforced in ``parse_args`` and again in ``PolitenessBudget``.
    Patching the single ``_pause`` indirection buys the speed and nothing else. Every run below
    therefore still records a 2.0-second posture, so no test in this file can produce a manifest
    that says the collector walked a merchant faster than it is allowed to.
    """
    monkeypatch.setattr(crc, "_pause", lambda _seconds: None)


def args_for(raw_dir: Path, *extra: str) -> Any:
    """``parse_args`` for a test run. The interval stays at the floor; ``free_waiting`` above is
    what stops that costing wall-clock."""
    return crc.parse_args(["fetch", "--raw-dir", str(raw_dir), *extra])


def hosts_file(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ======================================================================================
# the roster: --hosts-file
# ======================================================================================


def test_with_no_hosts_file_the_roster_is_still_the_built_in_ten(tmp_path: Path) -> None:
    """The committed corpus must stay reproducible from this script alone."""
    roster = crc.hosts_for(args_for(tmp_path))
    assert [spec.host for spec in roster] == [
        *crc.RELEVANT_HOSTS,
        *crc.NEGATIVE_CONTROL_HOSTS,
    ]
    assert [spec.role for spec in roster] == ["relevant"] * 8 + ["negative_control"] * 2
    assert {spec.category for spec in roster} == {crc.BUILTIN_CATEGORY}


def test_a_hosts_file_carries_the_category_beside_the_host(tmp_path: Path) -> None:
    """Category is the whole point: a corpus that cannot say what it spans cannot be checked."""
    path = hosts_file(
        tmp_path,
        "roster.txt",
        "# a comment line\n"
        "\n"
        "burrow.com    furniture\n"
        "  onyx.example   coffee   negative_control   # no furniture here\n"
        "homedepot.com tools off_platform_control\n",
    )
    roster = crc.parse_hosts_file(path)
    assert [(s.host, s.category, s.role) for s in roster] == [
        ("burrow.com", "furniture", "relevant"),
        ("onyx.example", "coffee", "negative_control"),
        ("homedepot.com", "tools", "off_platform_control"),
    ]
    assert roster[1].note == "no furniture here", "a host's trailing note is provenance"
    assert roster[0].source.endswith(":3"), "a spec remembers which line it came from"


def test_a_hosts_file_replaces_the_built_in_roster(tmp_path: Path) -> None:
    path = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    roster = crc.hosts_for(args_for(tmp_path, "--hosts-file", str(path)))
    assert [s.host for s in roster] == ["burrow.com"]


def test_hosts_files_compose_so_the_corpus_can_grow_without_dropping_its_stores(
    tmp_path: Path,
) -> None:
    """``--hosts-file`` is repeatable on purpose: the only way to add breadth to the corpus
    without losing the ten stores its current gates pin is to walk both rosters as one."""
    first = hosts_file(tmp_path, "a.txt", "burrow.com furniture\n")
    second = hosts_file(tmp_path, "b.txt", "onyx.example coffee\n")
    roster = crc.hosts_for(
        args_for(tmp_path, "--hosts-file", str(first), "--hosts-file", str(second))
    )
    assert [s.host for s in roster] == ["burrow.com", "onyx.example"]


def test_a_host_named_twice_is_an_error_rather_than_a_coin_toss(tmp_path: Path) -> None:
    """A duplicate would be walked twice and would silently take one of its two categories."""
    first = hosts_file(tmp_path, "a.txt", "burrow.com furniture\n")
    second = hosts_file(tmp_path, "b.txt", "burrow.com home-kitchen\n")
    with pytest.raises(SystemExit) as caught:
        crc.hosts_for(args_for(tmp_path, "--hosts-file", str(first), "--hosts-file", str(second)))
    assert "burrow.com" in str(caught.value)


@pytest.mark.parametrize(
    ("line", "because"),
    [
        ("burrow.com", "a host with no category"),
        ("burrow.com furniture relevant extra", "four fields"),
        ("Burrow.com furniture", "an upper-case host"),
        ("https://burrow.com furniture", "a URL rather than a hostname"),
        ("burrow.com/products furniture", "a path"),
        ("burrow.com Furniture", "an upper-case category"),
        ("burrow.com -furniture", "a category that does not start with a letter or digit"),
        ("burrow.com furniture maybe", "a role that is not one of the three"),
    ],
)
def test_a_malformed_roster_line_is_a_hard_error_naming_the_line(
    tmp_path: Path, line: str, because: str
) -> None:
    """Nothing is skipped-with-a-warning. A roster that quietly drops the line you meant to
    add is the same silent-empty failure ``--only`` used to have, one layer down."""
    path = hosts_file(tmp_path, "roster.txt", f"# header\n{line}\n")
    with pytest.raises(SystemExit) as caught:
        crc.parse_hosts_file(path)
    assert ":2:" in str(caught.value), f"{because}: the error must name the line number"


def test_a_roster_file_with_no_hosts_is_an_error(tmp_path: Path) -> None:
    """An empty roster and a roster of comments both fetch nothing; neither may exit 0."""
    path = hosts_file(tmp_path, "roster.txt", "# every line is a comment\n\n   \n")
    with pytest.raises(SystemExit) as caught:
        crc.parse_hosts_file(path)
    assert "no host lines" in str(caught.value)


def test_a_missing_roster_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        crc.parse_hosts_file(tmp_path / "nope.txt")
    assert "no such file" in str(caught.value)


# ======================================================================================
# the roster: --only, which used to return nothing and say nothing
# ======================================================================================


def test_only_composes_with_a_hosts_file_instead_of_returning_an_empty_roster(
    tmp_path: Path,
) -> None:
    """THE bug this ticket names. ``--only`` was filtered against the built-in tuple and
    nothing else, so ``--only allbirds.com`` gave an empty roster, fetched nothing and exited
    0 — indistinguishable from "nothing matched on purpose"."""
    path = hosts_file(tmp_path, "roster.txt", "allbirds.com apparel\nburrow.com furniture\n")
    roster = crc.hosts_for(args_for(tmp_path, "--hosts-file", str(path), "--only", "allbirds.com"))
    assert [(s.host, s.category) for s in roster] == [("allbirds.com", "apparel")]


def test_only_still_narrows_the_built_in_roster(tmp_path: Path) -> None:
    roster = crc.hosts_for(args_for(tmp_path, "--only", "toniiq.com"))
    assert [s.host for s in roster] == ["toniiq.com"]


@pytest.mark.parametrize("extra", [(), ("--hosts-file",)])
def test_only_naming_a_host_the_roster_does_not_carry_is_an_error(
    tmp_path: Path, extra: tuple[str, ...]
) -> None:
    """Not an empty roster, and not a silent ad-hoc fetch either: the roster IS the record of
    which businesses this crawler is allowed to touch, so the fix is to edit the roster."""
    argv = list(extra)
    if argv:
        argv.append(str(hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")))
    with pytest.raises(SystemExit) as caught:
        crc.hosts_for(args_for(tmp_path, *argv, "--only", "example.invalid"))
    message = str(caught.value)
    assert "example.invalid" in message and "--hosts-file" in message


def test_only_is_still_rejected_when_it_is_not_a_hostname(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        args_for(tmp_path, "--only", "https://burrow.com/products.json")


# ======================================================================================
# the checked-in rosters
# ======================================================================================


def test_the_incumbent_roster_file_restates_the_built_in_tuples_exactly() -> None:
    """``incumbent-hosts.txt`` is corpus provenance, and it is only provenance while it agrees
    with the code. This is what stops the two drifting."""
    assert INCUMBENT_HOSTS.is_file(), f"{INCUMBENT_HOSTS} is missing"
    assert crc.hosts_file_matches_builtin_roster(INCUMBENT_HOSTS)


def test_the_candidate_roster_is_broad_enough_to_be_worth_collecting() -> None:
    """The demo returns liver supplements for "a walnut coffee table" because all ten stores
    in the corpus sell supplements. This file is the fix, so its breadth is asserted here
    rather than eyeballed in review."""
    assert CANDIDATE_HOSTS.is_file(), f"{CANDIDATE_HOSTS} is missing"
    roster = crc.parse_hosts_file(CANDIDATE_HOSTS)
    assert 30 <= len(roster) <= 45, f"{len(roster)} hosts is outside the 30-45 the brief asks for"
    by_category: dict[str, list[str]] = {}
    for spec in roster:
        by_category.setdefault(spec.category, []).append(spec.host)
    assert len(by_category) >= 8, f"only {len(by_category)} categories: {sorted(by_category)}"
    thin = {name: hosts for name, hosts in by_category.items() if len(hosts) < 2}
    assert not thin, f"a one-store category cannot show a spread: {thin}"
    assert "furniture" in by_category, "the query that fails today has no candidates"


def test_the_candidate_roster_names_its_deliberate_misses() -> None:
    """A skip means two different things — "this merchant does not serve the endpoint" and
    "we failed" — unless the roster says in advance which hosts are expected to miss."""
    roster = crc.parse_hosts_file(CANDIDATE_HOSTS)
    misses = [s for s in roster if s.role == "off_platform_control"]
    assert len(misses) >= 2, "no deliberate misses: every skip in the run would be ambiguous"
    for spec in misses:
        assert spec.note, f"{spec.host} is a control with no recorded reason"


def test_the_candidate_roster_does_not_re_collect_the_incumbents() -> None:
    """Both files are walked together via a repeatable ``--hosts-file``; an overlap would be a
    duplicate-host error at roster time, i.e. the whole run refuses to start."""
    candidates = {s.host for s in crc.parse_hosts_file(CANDIDATE_HOSTS)}
    incumbents = {s.host for s in crc.parse_hosts_file(INCUMBENT_HOSTS)}
    assert not candidates & incumbents


def test_the_two_checked_in_rosters_can_be_walked_as_one(tmp_path: Path) -> None:
    roster = crc.hosts_for(
        args_for(
            tmp_path, "--hosts-file", str(INCUMBENT_HOSTS), "--hosts-file", str(CANDIDATE_HOSTS)
        )
    )
    assert len(roster) == 10 + len(crc.parse_hosts_file(CANDIDATE_HOSTS))
    assert len({s.host for s in roster}) == len(roster)


# ======================================================================================
# resume: what "already collected" is allowed to mean
# ======================================================================================


@pytest.mark.parametrize(
    "outcome",
    ["exhausted", "page_cap", "budget", "robots_disallowed", "http_404", "http_403", "http_410"],
)
def test_an_ending_that_is_an_answer_is_not_retried(outcome: str) -> None:
    """A short page, a page cap, a robots decision, a 404 — the merchant answered. Re-asking
    is the impoliteness this whole mechanism exists to avoid."""
    assert crc.outcome_is_retryable(outcome) is False


@pytest.mark.parametrize(
    "outcome",
    [
        "interrupted",
        "transport_error",
        "empty_body",
        "unparseable_page",
        "robots_transport_error",
        "robots_budget",
        "http_429",
        "http_500",
        "http_503",
        "robots_http_429",
        "robots_http_502",
        "something_a_later_version_invented",
    ],
)
def test_an_ending_that_is_a_bad_minute_is_retried(outcome: str) -> None:
    """429 is the one that matters most: a Cloudflare block recorded as "this merchant serves
    nothing" would record the crawler's own bad behaviour as a fact about somebody's shop.

    The last case is the important one for the future — an outcome this version does not
    understand fails CLOSED, so a new ending added later cannot be reused by an old resume."""
    assert crc.outcome_is_retryable(outcome) is True


@pytest.fixture
def raw_store(tmp_path: Path) -> Iterator[Callable[..., tuple[Path, Any, dict[str, Any]]]]:
    """Build a raw directory holding one finished store, and hand back its pieces."""

    def make(outcome: str = "exhausted") -> tuple[Path, Any, dict[str, Any]]:
        raw_dir = tmp_path / "raw"
        args = args_for(raw_dir)
        body = json.dumps({"products": []}, separators=(",", ":")).encode()
        (raw_dir / "burrow.com").mkdir(parents=True)
        (raw_dir / "burrow.com" / "page-001.json").write_bytes(body)
        entry = {
            "host": "burrow.com",
            "role": "relevant",
            "category": "furniture",
            "walk_outcome": outcome,
            "pages": [
                {
                    "page": 1,
                    "file": "burrow.com/page-001.json",
                    "url": "https://burrow.com/products.json?limit=250&page=1",
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "fetched_at": "2026-09-08T00:00:00+00:00",
                    "bytes": len(body),
                    "products": 0,
                }
            ],
        }
        crc.write_store_record(raw_dir, entry, crc.fetch_fingerprint(args))
        return raw_dir, args, entry

    yield make


def test_a_finished_store_is_reused(raw_store: Any) -> None:
    raw_dir, args, _ = raw_store()
    entry, why_not = crc.reusable_entry(raw_dir, "burrow.com", crc.fetch_fingerprint(args))
    assert why_not == ""
    assert entry is not None and entry["host"] == "burrow.com"


def test_a_store_with_no_record_is_not_reused(tmp_path: Path) -> None:
    entry, why_not = crc.reusable_entry(tmp_path, "burrow.com", {})
    assert entry is None and "no store record" in why_not


def test_a_store_whose_walk_was_interrupted_is_never_mistaken_for_a_finished_one(
    raw_store: Any,
) -> None:
    """``interrupted`` is the DEFAULT ``walk_outcome``, so a store whose walk never reached an
    ending — because the process was killed mid-page — cannot read as finished."""
    raw_dir, args, _ = raw_store(outcome="interrupted")
    entry, why_not = crc.reusable_entry(raw_dir, "burrow.com", crc.fetch_fingerprint(args))
    assert entry is None and "retryable" in why_not


def test_a_store_that_ended_on_a_429_is_walked_again(raw_store: Any) -> None:
    raw_dir, args, _ = raw_store(outcome="http_429")
    entry, why_not = crc.reusable_entry(raw_dir, "burrow.com", crc.fetch_fingerprint(args))
    assert entry is None and "http_429" in why_not


def test_raising_the_page_cap_invalidates_every_record_taken_under_the_old_one(
    raw_store: Any, tmp_path: Path
) -> None:
    """A store truncated at 40 pages is 'complete' only for as long as the cap is 40. Reusing
    it after the cap rises would keep a partial catalogue wearing a whole catalogue's label."""
    raw_dir, _, _ = raw_store()
    wider = args_for(raw_dir, "--max-pages", "80")
    entry, why_not = crc.reusable_entry(raw_dir, "burrow.com", crc.fetch_fingerprint(wider))
    assert entry is None and "settings changed" in why_not


def test_a_record_whose_page_file_is_gone_is_not_reused(raw_store: Any) -> None:
    raw_dir, args, _ = raw_store()
    (raw_dir / "burrow.com" / "page-001.json").unlink()
    entry, why_not = crc.reusable_entry(raw_dir, "burrow.com", crc.fetch_fingerprint(args))
    assert entry is None and "is gone" in why_not


def test_a_record_whose_page_bytes_changed_is_not_reused(raw_store: Any) -> None:
    """The poison case. A page truncated mid-product still parses as a file and still has the
    right name; only the digest knows. Without this check the store would be skipped and half
    a catalogue would enter the corpus labelled complete."""
    raw_dir, args, _ = raw_store()
    page = raw_dir / "burrow.com" / "page-001.json"
    page.write_bytes(page.read_bytes()[:-3])
    entry, why_not = crc.reusable_entry(raw_dir, "burrow.com", crc.fetch_fingerprint(args))
    assert entry is None and "digest" in why_not


def test_a_record_that_does_not_parse_is_not_reused(raw_store: Any) -> None:
    raw_dir, args, _ = raw_store()
    (raw_dir / "burrow.com" / crc.STORE_RECORD).write_text("{not json", encoding="utf-8")
    entry, why_not = crc.reusable_entry(raw_dir, "burrow.com", crc.fetch_fingerprint(args))
    assert entry is None and "unreadable" in why_not


def test_the_record_is_written_atomically_and_leaves_no_partial_behind(raw_store: Any) -> None:
    """The mechanism turns on "a record that exists is a record that parses", so a half-written
    record must not be reachable under the record's own name."""
    raw_dir, _, _ = raw_store()
    store_dir = raw_dir / "burrow.com"
    assert (store_dir / crc.STORE_RECORD).is_file()
    assert not list(store_dir.glob("*.partial")), "a temp file survived the rename"
    record = json.loads((store_dir / crc.STORE_RECORD).read_text(encoding="utf-8"))
    assert record["schema"] == crc.STORE_RECORD_SCHEMA
    assert record["complete"] is True
    assert record["fingerprint"]["page_size"] == 250


# ======================================================================================
# resume, driven through the real fetch loop
# ======================================================================================


def _roster_file(tmp_path: Path) -> Path:
    return hosts_file(tmp_path, "roster.txt", "burrow.com furniture\nonyx.example coffee\n")


def test_a_second_run_re_fetches_nothing_it_already_has(tmp_path: Path, net: Any) -> None:
    """The politeness claim, driven through ``run_fetch`` rather than argued for in a comment:
    a resumed run makes ZERO requests against stores it already walked."""
    roster = _roster_file(tmp_path)
    raw = tmp_path / "raw"
    hosts = {"burrow.com": Storefront(products=3), "onyx.example": Storefront(products=2)}

    first = net(hosts)
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert len(first.requests) == 4, "two robots fetches and two catalogue pages"

    second = net(hosts)
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert second.requests == [], f"a resumed run hit the network: {second.requests}"
    # ...and the second run's tally is the one that was actually wired up. Without this, an
    # empty `second.requests` could equally mean the fake was never installed.
    assert len(first.requests) == 4, "the second install did not replace the first"
    assert log["reused_from_earlier_runs"] == ["burrow.com", "onyx.example"]
    assert log["complete"] is True
    assert [s["host"] for s in log["stores"]] == ["burrow.com", "onyx.example"]


def test_no_resume_walks_everything_again(tmp_path: Path, net: Any) -> None:
    roster = _roster_file(tmp_path)
    raw = tmp_path / "raw"
    hosts = {"burrow.com": Storefront(products=3), "onyx.example": Storefront(products=2)}
    net(hosts)
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    again = net(hosts)
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster), "--no-resume"))
    assert len(again.requests) == 4


def test_a_store_blocked_by_a_429_is_the_only_one_re_walked(tmp_path: Path, net: Any) -> None:
    """The shape a real interrupted run takes: some stores finished, one was rate-limited.
    The finished ones must not be touched again and the blocked one must be."""
    roster = _roster_file(tmp_path)
    raw = tmp_path / "raw"
    blocked = {
        "burrow.com": Storefront(products=3),
        "onyx.example": Storefront(products=2, page_status={1: 429}),
    }
    net(blocked)
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))

    healed = {"burrow.com": Storefront(products=3), "onyx.example": Storefront(products=2)}
    second = net(healed)
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert log["reused_from_earlier_runs"] == ["burrow.com"]
    assert healed["burrow.com"].requests == [], "a finished store was hit again"
    assert len(second.requests) == 2, "the blocked store cost one robots fetch and one page"
    onyx = next(s for s in log["stores"] if s["host"] == "onyx.example")
    assert onyx["skipped"] is None and onyx["walk_outcome"] == "exhausted"


def test_a_merchant_who_said_no_is_not_asked_again(tmp_path: Path, net: Any) -> None:
    """A robots.txt disallow is an ANSWER, so it is complete and it is reused. Re-asking a
    merchant who has already refused is the thing the resume must not make cheaper."""
    roster = _roster_file(tmp_path)
    raw = tmp_path / "raw"
    hosts = {
        "burrow.com": Storefront(products=3),
        "onyx.example": Storefront(robots="User-agent: *\nDisallow: /products.json\n"),
    }
    net(hosts)
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert log["stores"][1]["walk_outcome"] == "robots_disallowed"
    second = net(hosts)
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert second.requests == []
    assert log["reused_from_earlier_runs"] == ["burrow.com", "onyx.example"]


def test_a_robots_fetch_that_failed_transport_is_asked_again(tmp_path: Path, net: Any) -> None:
    """A 503 on robots.txt is a machine having a bad minute, not the merchant saying no. The
    two must not leave the same record — one is honoured forever, the other is retried."""
    roster = _roster_file(tmp_path)
    raw = tmp_path / "raw"
    net({"burrow.com": Storefront(products=3), "onyx.example": Storefront(robots_status=503)})
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert log["stores"][1]["walk_outcome"] == "robots_http_503"
    second = net({"burrow.com": Storefront(products=3), "onyx.example": Storefront(products=2)})
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert log["reused_from_earlier_runs"] == ["burrow.com"]
    assert len(second.requests) == 2


@pytest.mark.parametrize("robots_status", [403, 429, 503])
def test_a_robots_file_that_could_not_be_read_is_not_recorded_as_a_refusal(
    tmp_path: Path, net: Any, robots_status: int
) -> None:
    """The skip REASON must not say the merchant refused when robots.txt was never read.

    Observed live on 2026-09-08: katzmosestools.com dropped the connection and bombas.com
    answered 429, and both were written into the log as ``robots.txt disallows
    https://.../products.json`` — a sentence that turns our own bad minute into their stated
    policy, and one the record beside it contradicts, since ``walk_outcome`` marks both
    retryable. A reader triaging the corpus by its reasons would drop two live storefronts as
    having said no.
    """
    roster = _roster_file(tmp_path)
    net(
        {
            "burrow.com": Storefront(products=3),
            "onyx.example": Storefront(robots_status=robots_status),
        }
    )
    log = crc.run_fetch(args_for(tmp_path / "raw", "--hosts-file", str(roster)))
    reason = log["stores"][1]["skipped"]["reason"]
    assert "disallow" not in reason.lower(), (
        f"a robots.txt that answered {robots_status} was recorded as a refusal: {reason!r}"
    )
    assert "could not be read" in reason
    assert str(robots_status) in reason, "the reason must still say what actually happened"


def test_a_robots_file_that_was_read_and_said_no_still_says_disallows(
    tmp_path: Path, net: Any
) -> None:
    """The other direction, so the fix above cannot be satisfied by never saying `disallows`.
    A merchant who published a rule against this path gets that recorded in those words."""
    roster = _roster_file(tmp_path)
    net(
        {
            "burrow.com": Storefront(products=3),
            "onyx.example": Storefront(robots="User-agent: *\nDisallow: /products.json\n"),
        }
    )
    log = crc.run_fetch(args_for(tmp_path / "raw", "--hosts-file", str(roster)))
    store = log["stores"][1]
    assert store["walk_outcome"] == "robots_disallowed"
    assert "disallows" in store["skipped"]["reason"]


def test_a_crash_partway_leaves_a_log_that_build_can_read(tmp_path: Path, net: Any) -> None:
    """``fetchlog.json`` used to be written once, after the LAST store, so a crash at store 900
    left a raw directory full of pages and no log, and ``build`` died on FileNotFoundError.
    It is now rewritten after every store, and it says it is not finished."""
    roster = hosts_file(
        tmp_path, "roster.txt", "burrow.com furniture\nboom.example coffee\nlast.example pet\n"
    )
    raw = tmp_path / "raw"

    class Exploding(FakeInternet):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.host.endswith("boom.example"):
                raise KeyboardInterrupt("the operator gave up at store two")
            return super().handler(request)

    fake = Exploding({"burrow.com": Storefront(products=3), "last.example": Storefront()})
    real_client = httpx.Client
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            httpx,
            "Client",
            lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(fake.handler)}),
        )
        with pytest.raises(KeyboardInterrupt):
            crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))

    log = json.loads((raw / "fetchlog.json").read_text(encoding="utf-8"))
    assert [s["host"] for s in log["stores"]] == ["burrow.com"]
    assert log["complete"] is False
    assert log["pending"] == ["boom.example", "last.example"]

    # ...and the run picks up where it stopped, without re-hitting the store that finished.
    healed = net(
        {
            "burrow.com": Storefront(products=3),
            "boom.example": Storefront(products=1),
            "last.example": Storefront(products=1),
        }
    )
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert log["complete"] is True
    assert log["reused_from_earlier_runs"] == ["burrow.com"]
    assert len(healed.requests) == 4, "only the two unfinished stores were walked"


def test_build_says_what_to_do_when_there_is_no_log_at_all(tmp_path: Path) -> None:
    """The bare ``FileNotFoundError`` this replaced said neither what was wrong nor what the
    operator should do with a raw directory that cost them an hour."""
    raw = tmp_path / "raw"
    raw.mkdir()
    with pytest.raises(SystemExit) as caught:
        crc.load_fetchlog(raw)
    assert "no store was walked" in str(caught.value)


# ======================================================================================
# build: the corpus knows which categories it spans
# ======================================================================================


def test_the_built_corpus_reports_its_categories_and_its_deliberate_misses(
    tmp_path: Path, net: Any
) -> None:
    """The breadth claim has to be readable off the corpus, not recomputed by whoever asks."""
    roster = hosts_file(
        tmp_path,
        "roster.txt",
        "burrow.com furniture\n"
        "sofa.example furniture\n"
        "onyx.example coffee\n"
        "bigbox.example tools off_platform_control\n",
    )
    raw, out = tmp_path / "raw", tmp_path / "out"
    net(
        {
            "burrow.com": Storefront(products=3),
            "sofa.example": Storefront(products=2),
            "onyx.example": Storefront(products=4),
            # The deliberate miss: robots allows, the endpoint simply is not there.
            "bigbox.example": Storefront(page_status={1: 404}),
        }
    )
    exit_code = crc.main(
        [
            "all",
            "--raw-dir",
            str(raw),
            "--out",
            str(out),
            "--hosts-file",
            str(roster),
            "--no-compress",
        ]
    )
    assert exit_code == 0
    manifest = json.loads((out / "collection.json").read_text(encoding="utf-8"))

    assert manifest["categories"]["furniture"]["products"] == 5
    assert manifest["categories"]["coffee"]["products"] == 4
    assert manifest["categories"]["furniture"]["stores"] == ["burrow.com", "sofa.example"]
    assert manifest["totals"]["categories"] == 2, "the control has no inventory and is not one"

    assert manifest["totals"]["off_platform_controls"] == 1
    assert manifest["totals"]["off_platform_controls_that_answered"] == 0
    control = next(s for s in manifest["stores"] if s["host"] == "bigbox.example")
    assert control["skipped"] is not None, "the deliberate miss must be recorded as a skip"
    assert control["walk_outcome"] == "http_404"

    assert manifest["run"]["complete"] is True
    assert manifest["run"]["pending"] == []
    assert manifest["collected_span"]["earliest"] and manifest["collected_span"]["latest"]
    assert manifest["per_store_counts"] == {
        "burrow.com": 3,
        "sofa.example": 2,
        "onyx.example": 4,
    }
    # The corpus is still what it always was: verbatim bytes with row-aligned provenance.
    lines = (out / "stores" / "burrow.com.products.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["handle"] == "burrow-0"


def test_a_corpus_built_from_a_partial_run_never_looks_complete(tmp_path: Path, net: Any) -> None:
    """A partial corpus is a legitimate thing to build. A partial corpus that READS as whole
    is how a store quietly disappears from the roster and nobody notices."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\nghost.example pet\n")
    raw, out = tmp_path / "raw", tmp_path / "out"
    net({"burrow.com": Storefront(products=3), "ghost.example": Storefront(products=1)})
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster), "--only", "burrow.com"))

    log = json.loads((raw / "fetchlog.json").read_text(encoding="utf-8"))
    args = crc.parse_args(["build", "--raw-dir", str(raw), "--out", str(out), "--no-compress"])
    manifest = crc.run_build(args, log)
    assert manifest["run"]["complete"] is True, "--only narrowed the roster; that run finished"
    # ...and the narrowing is on the record. A one-store corpus that finished is legitimate;
    # a one-store corpus that does not say why it is one store is not.
    assert manifest["run"]["restricted_to"] == ["burrow.com"]
    assert manifest["run"]["roster_size"] == 1
    assert manifest["per_store_counts"] == {"burrow.com": 3}

    # Now the honest partial: hand `build` a log that knows two stores were on the roster.
    log["complete"] = False
    log["pending"] = ["ghost.example"]
    log["roster"] = [{"host": "burrow.com"}, {"host": "ghost.example"}]
    manifest = crc.run_build(args, log)
    assert manifest["run"]["complete"] is False
    assert manifest["run"]["pending"] == ["ghost.example"]
    assert manifest["run"]["roster_size"] == 2


def test_the_corpus_still_compresses_and_round_trips(tmp_path: Path, net: Any) -> None:
    """Compression is a storage decision and nothing more: the gzip must hold the same bytes."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw, out = tmp_path / "raw", tmp_path / "out"
    net({"burrow.com": Storefront(products=2)})
    crc.main(
        [
            "all",
            "--raw-dir",
            str(raw),
            "--out",
            str(out),
            "--hosts-file",
            str(roster),
        ]
    )
    blob = gzip.decompress((out / "stores" / "burrow.com.products.jsonl.gz").read_bytes())
    assert [json.loads(line)["id"] for line in blob.splitlines()] == [1_000_000, 1_000_001]
    manifest = json.loads((out / "collection.json").read_text(encoding="utf-8"))
    store = manifest["stores"][0]
    on_disk = (out / store["files"]["products"]).read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == store["file_sha256"]["products"]


# ======================================================================================
# the rule the whole suite lives under
# ======================================================================================


def test_the_whole_fetch_build_round_trip_opens_no_socket(
    tmp_path: Path, net: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proved, not asserted: ``socket.socket`` is replaced with something that explodes, and
    the full fetch+build still runs. Collection stays a by-hand operation (D3/C9)."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw, out = tmp_path / "raw", tmp_path / "out"
    net({"burrow.com": Storefront(products=2)})

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the collector's test suite opened a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert (
        crc.main(
            [
                "all",
                "--raw-dir",
                str(raw),
                "--out",
                str(out),
                "--hosts-file",
                str(roster),
                "--no-compress",
            ]
        )
        == 0
    )
    assert (out / "collection.json").is_file()


# ======================================================================================
# the politeness floor: 2.0 seconds is a FLOOR, not a default
# ======================================================================================


@pytest.mark.parametrize("value", ["0", "0.0", "1.9", "-3"])
def test_an_interval_below_the_floor_is_refused_rather_than_silently_honoured(
    tmp_path: Path, value: str
) -> None:
    """``--min-interval 0`` used to be accepted in silence and ``PolitenessBudget`` obeyed it.

    Silence is the whole defect: ``fetch_fingerprint`` deliberately excludes the interval, so a
    resumed run could splice zero-second pages into a corpus whose manifest declares 2.0 with
    nothing on disk recording that it happened. The only guard was an assertion on the committed
    corpus, which runs long after the requests were made.
    """
    with pytest.raises(SystemExit) as caught:
        crc.parse_args(["fetch", "--raw-dir", str(tmp_path), "--min-interval", value])
    assert caught.value.code == 2


def test_the_floor_is_not_clamped_away(tmp_path: Path) -> None:
    """Rejected, not quietly raised to 2.0. A clamp makes the manifest agree with the traffic by
    ignoring what was asked for, and leaves no way to tell a clamped run from a polite one."""
    with pytest.raises(SystemExit):
        crc.parse_args(["fetch", "--raw-dir", str(tmp_path), "--min-interval", "0.5"])
    args = crc.parse_args(["fetch", "--raw-dir", str(tmp_path)])
    assert args.min_interval == crc.MIN_INTERVAL_FLOOR
    wider = crc.parse_args(["fetch", "--raw-dir", str(tmp_path), "--min-interval", "5"])
    assert wider.min_interval == 5.0, "widening the gap must still be allowed"


def test_the_budget_itself_refuses_an_impolite_interval() -> None:
    """The CLI is not the only caller. A floor that lives only in argument parsing is a default
    wearing a floor's name."""
    with pytest.raises(ValueError, match="politeness floor"):
        crc.PolitenessBudget(min_interval=0.0, max_requests=45)
    with pytest.raises(ValueError, match="politeness floor"):
        crc.PolitenessBudget(min_interval=1.99, max_requests=45)


def test_the_budget_actually_waits_the_interval_between_two_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap is enforced by sleeping, so the test watches the sleep rather than the clock."""
    waits: list[float] = []
    monkeypatch.setattr(crc, "_pause", waits.append)
    clock = iter([100.0, 100.4, 100.4])
    monkeypatch.setattr(crc.time, "monotonic", lambda: next(clock))
    budget = crc.PolitenessBudget(min_interval=crc.MIN_INTERVAL_FLOOR, max_requests=45)
    assert budget.spend("burrow.com") is True
    assert waits == [], "the first request to a host waits for nothing"
    assert budget.spend("www.burrow.com") is True
    assert waits and waits[0] == pytest.approx(1.6), (
        f"0.4s after the first hit, the second must wait the remaining 1.6s; waited {waits}"
    )


def test_a_declared_crawl_delay_can_widen_the_interval_but_never_narrow_it() -> None:
    budget = crc.PolitenessBudget(min_interval=crc.MIN_INTERVAL_FLOOR, max_requests=45)
    budget.honour_crawl_delay("burrow.com", 0.1)
    assert budget.interval_for("burrow.com") == crc.MIN_INTERVAL_FLOOR
    budget.honour_crawl_delay("burrow.com", 9.0)
    assert budget.interval_for("burrow.com") == 9.0


def test_the_built_manifest_declares_the_floor_it_was_walked_under(
    tmp_path: Path, net: Any
) -> None:
    """The manifest is where a reader learns the posture, so the floor has to reach it."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw, out = tmp_path / "raw", tmp_path / "out"
    net({"burrow.com": Storefront(products=2)})
    crc.main(
        [
            "all",
            "--raw-dir",
            str(raw),
            "--out",
            str(out),
            "--hosts-file",
            str(roster),
            "--no-compress",
        ]
    )
    manifest = json.loads((out / "collection.json").read_text(encoding="utf-8"))
    politeness = manifest["politeness"]
    assert politeness["min_seconds_between_requests_per_host"] >= crc.MIN_INTERVAL_FLOOR
    assert politeness["min_interval_floor_seconds"] == crc.MIN_INTERVAL_FLOOR
    store = manifest["stores"][0]
    assert store["robots"]["crawl_delay_seconds"] >= crc.MIN_INTERVAL_FLOOR


# ======================================================================================
# the resume gate: the OUTCOME decides, not the boolean stored beside it
# ======================================================================================


def _hand_written_record(
    raw_dir: Path, host: str, fingerprint: dict[str, Any], **overrides: Any
) -> Path:
    """A store record written straight to disk, bypassing ``write_store_record``.

    This is the fixture the resume gate never had, and its absence is why the gate could be
    vacuous without a single test going red: every existing resume fixture writes through
    ``write_store_record``, which DERIVES ``complete`` from ``walk_outcome``, so the two fields
    can never disagree there. A record whose fields contradict each other is what a hand edit,
    a partial merge or a later collector version actually leaves behind, and it is the only
    input that can tell "the outcome is consulted" apart from "the boolean is trusted".
    """
    body = json.dumps({"products": []}, separators=(",", ":")).encode()
    store_dir = raw_dir / host
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "page-001.json").write_bytes(body)
    record: dict[str, Any] = {
        "schema": crc.STORE_RECORD_SCHEMA,
        "recorded_at": "2026-09-08T00:00:00+00:00",
        "walk_outcome": "exhausted",
        "complete": True,
        "fingerprint": fingerprint,
        "entry": {
            "host": host,
            "role": "relevant",
            "category": "furniture",
            "walk_outcome": "exhausted",
            "pages": [
                {
                    "page": 1,
                    "file": f"{host}/page-001.json",
                    "url": f"https://{host}/products.json?limit=250&page=1",
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "fetched_at": "2026-09-08T00:00:00+00:00",
                    "bytes": len(body),
                    "products": 0,
                }
            ],
        },
    }
    record.update(overrides)
    path = store_dir / crc.STORE_RECORD
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "outcome",
    [
        "interrupted",
        "transport_error",
        "empty_body",
        "http_429",
        "robots_transport_error",
        "ended_politely_v3",
    ],
)
def test_a_record_claiming_complete_over_a_retryable_outcome_is_refused(
    tmp_path: Path, outcome: str
) -> None:
    """The defect, exactly. ``reusable_entry`` trusted ``complete`` and never called
    ``outcome_is_retryable``, so a record saying ``{"complete": true, "walk_outcome":
    "interrupted"}`` — or ``transport_error``, or an outcome no version has ever emitted — was
    REUSED with no request made. The advertised three-way fail-closed protection existed only on
    the write path, where nothing could ever trip it.

    ``ended_politely_v3`` is the future-version case: only ``collector_version`` inside the
    fingerprint covered it incidentally, and only if whoever invented the outcome remembered to
    bump ``CORPUS_VERSION``.
    """
    args = args_for(tmp_path / "raw")
    fingerprint = crc.fetch_fingerprint(args)
    _hand_written_record(
        tmp_path / "raw", "burrow.com", fingerprint, walk_outcome=outcome, complete=True
    )
    entry, why_not = crc.reusable_entry(tmp_path / "raw", "burrow.com", fingerprint)
    assert entry is None, f"a record with outcome {outcome!r} was reused on the say-so of a flag"
    assert outcome in why_not and "retryable" in why_not


def test_a_record_that_disagrees_with_itself_the_other_way_is_also_refused(
    tmp_path: Path,
) -> None:
    """``complete: false`` beside a terminal outcome is the same damage from the other side, and
    "believe the outcome" alone would silently repair it. A record whose two fields contradict
    each other is not a record."""
    args = args_for(tmp_path / "raw")
    fingerprint = crc.fetch_fingerprint(args)
    _hand_written_record(
        tmp_path / "raw", "burrow.com", fingerprint, walk_outcome="exhausted", complete=False
    )
    entry, why_not = crc.reusable_entry(tmp_path / "raw", "burrow.com", fingerprint)
    assert entry is None and "disagrees with itself" in why_not


def test_a_record_with_no_outcome_at_all_is_refused(tmp_path: Path) -> None:
    args = args_for(tmp_path / "raw")
    fingerprint = crc.fetch_fingerprint(args)
    path = _hand_written_record(tmp_path / "raw", "burrow.com", fingerprint)
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["walk_outcome"]
    path.write_text(json.dumps(record), encoding="utf-8")
    entry, why_not = crc.reusable_entry(tmp_path / "raw", "burrow.com", fingerprint)
    assert entry is None and "does not say why the walk ended" in why_not


def test_a_hand_written_record_that_agrees_with_itself_is_still_reused(tmp_path: Path) -> None:
    """The silent-on-honest direction, so the fix above cannot be satisfied by refusing
    everything that did not come out of this process."""
    args = args_for(tmp_path / "raw")
    fingerprint = crc.fetch_fingerprint(args)
    _hand_written_record(tmp_path / "raw", "burrow.com", fingerprint)
    entry, why_not = crc.reusable_entry(tmp_path / "raw", "burrow.com", fingerprint)
    assert why_not == ""
    assert entry is not None and entry["host"] == "burrow.com"


def test_a_forged_complete_record_costs_a_walk_rather_than_a_half_catalogue(
    tmp_path: Path, net: Any
) -> None:
    """End to end, through ``run_fetch``: the store is re-walked and the run says why."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw = tmp_path / "raw"
    args = args_for(raw, "--hosts-file", str(roster))
    _hand_written_record(
        raw, "burrow.com", crc.fetch_fingerprint(args), walk_outcome="interrupted", complete=True
    )
    fake = net({"burrow.com": Storefront(products=3)})
    log = crc.run_fetch(args)
    assert log["reused_from_earlier_runs"] == [], "the forged record was believed"
    assert len(fake.requests) == 2, "the store must be re-walked: one robots fetch, one page"
    assert log["stores"][0]["walk_outcome"] == "exhausted"


# ======================================================================================
# a failed walk must not destroy what a successful earlier walk collected
# ======================================================================================


class Unreachable(FakeInternet):
    """Every request raises, the way a blocked egress IP behaves."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        raise httpx.ConnectError("connection refused")


@pytest.fixture
def blocked_net(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Unreachable]:
    def install() -> Unreachable:
        fake = Unreachable({})

        def client(**kwargs: Any) -> httpx.Client:
            kwargs["transport"] = httpx.MockTransport(fake.handler)
            return _REAL_HTTPX_CLIENT(**kwargs)

        monkeypatch.setattr(httpx, "Client", client)
        return fake

    return install


def test_a_walk_that_fails_on_robots_keeps_the_pages_an_earlier_walk_collected(
    tmp_path: Path, net: Any, blocked_net: Any
) -> None:
    """Measured on the real thing before this was fixed: with a client raising ``ConnectError``,
    floydhome.com's 3.7 MB ``page-001.json`` was deleted and the walk then failed on robots.

    The stale-page sweep ran straight after ``store_dir.mkdir`` — before the robots fetch and
    before any request — so one ``--no-resume`` run, or one ``--max-pages`` bump, made while the
    egress IP happened to be blocked emptied the raw directory host by host while collecting
    nothing. That directory is what makes "promotion is offline and costs the merchants nothing"
    true, so the defect converted one bad minute into re-fetching every merchant.
    """
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw = tmp_path / "raw"
    net({"burrow.com": Storefront(products=3)})
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    page = raw / "burrow.com" / "page-001.json"
    good_bytes = page.read_bytes()
    assert good_bytes

    blocked_net()
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster), "--no-resume"))
    assert page.is_file(), "a walk that never got past robots.txt deleted a good page"
    assert page.read_bytes() == good_bytes


def test_a_failed_walk_does_not_replace_a_finished_record(
    tmp_path: Path, net: Any, blocked_net: Any
) -> None:
    """Preserving the pages is not enough on its own: the record is what the resume reads, so a
    failure that clobbers it makes the surviving bytes unreachable and the merchant is walked
    again anyway."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw = tmp_path / "raw"
    net({"burrow.com": Storefront(products=3)})
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))

    blocked_net()
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster), "--no-resume"))
    record = json.loads((raw / "burrow.com" / crc.STORE_RECORD).read_text(encoding="utf-8"))
    assert record["complete"] is True and record["walk_outcome"] == "exhausted"
    assert log["stores"][0]["skipped"] is None, "the failed walk was logged over a good store"

    # ...the failure is kept rather than swallowed...
    parked = json.loads((raw / "burrow.com" / crc.FAILED_WALK_RECORD).read_text(encoding="utf-8"))
    assert parked["walk_outcome"] == "robots_transport_error"
    assert parked["complete"] is False

    # ...and the next resume costs the merchant nothing.
    healed = net({"burrow.com": Storefront(products=3)})
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert healed.requests == []
    assert log["reused_from_earlier_runs"] == ["burrow.com"]


def test_a_longer_earlier_walk_still_gets_its_stale_pages_swept(tmp_path: Path, net: Any) -> None:
    """The hygiene the old sweep bought must survive the fix: once a record that does not claim
    them is on disk, a longer earlier walk's pages go. A ``page-002.json`` beside a one-page
    record is exactly the kind of thing that gets believed."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw = tmp_path / "raw"
    net({"burrow.com": Storefront(products=5)}, page_size=2)
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster), "--page-size", "2"))
    assert sorted(p.name for p in (raw / "burrow.com").glob("page-*.json")) == [
        "page-001.json",
        "page-002.json",
        "page-003.json",
    ]

    net({"burrow.com": Storefront(products=1)}, page_size=2)
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster), "--page-size", "2", "--no-resume"))
    assert sorted(p.name for p in (raw / "burrow.com").glob("page-*.json")) == ["page-001.json"]


# ======================================================================================
# requests: a number that can be read back off the artifact
# ======================================================================================


def test_the_recorded_request_count_matches_what_the_merchants_were_actually_asked(
    tmp_path: Path, net: Any
) -> None:
    """``totals.requests_made`` was ``sum(len(fetches) + 1)`` over one record per store, and a
    re-walk replaces its own record, so the number structurally could not see one. Here a host
    is walked twice — blocked, then healed — and the total must include both walks."""
    roster = _roster_file(tmp_path)
    raw, out = tmp_path / "raw", tmp_path / "out"
    first = net(
        {
            "burrow.com": Storefront(products=3),
            "onyx.example": Storefront(products=2, page_status={1: 429}),
        }
    )
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    second = net({"burrow.com": Storefront(products=3), "onyx.example": Storefront(products=2)})
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))

    really_asked = len(first.requests) + len(second.requests)
    assert really_asked == 6, f"expected 4 + 2 requests, saw {really_asked}"
    args = crc.parse_args(["build", "--raw-dir", str(raw), "--out", str(out), "--no-compress"])
    manifest = crc.run_build(args, log)
    assert manifest["totals"]["requests_recorded"] == really_asked
    assert "requests_made" not in manifest["totals"], "the name that overclaimed is gone"


def test_the_request_count_is_recomputable_from_the_stores_block(tmp_path: Path, net: Any) -> None:
    """A number published in an artifact must be readable back OUT of that artifact. This is the
    recipe ``politeness.request_accounting`` states, run against the artifact it describes."""
    roster = _roster_file(tmp_path)
    raw, out = tmp_path / "raw", tmp_path / "out"
    net({"burrow.com": Storefront(products=3), "onyx.example": Storefront(products=2)})
    log = crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    args = crc.parse_args(["build", "--raw-dir", str(raw), "--out", str(out), "--no-compress"])
    manifest = crc.run_build(args, log)
    recomputed = sum(int(s["requests_charged"]) for s in manifest["stores"])
    assert recomputed == manifest["totals"]["requests_recorded"]
    assert "request_accounting" in manifest["politeness"]


def test_a_page_the_budget_refused_is_not_counted_as_a_request(tmp_path: Path, net: Any) -> None:
    """A page the per-host cap refused is recorded in ``fetches`` with ``status: -1`` and the
    note "not fetched". ``len(fetches) + 1`` counted it, inflating what the merchant saw."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw = tmp_path / "raw"
    fake = net({"burrow.com": Storefront(products=10)}, page_size=2)
    log = crc.run_fetch(
        args_for(raw, "--hosts-file", str(roster), "--page-size", "2", "--max-requests", "3")
    )
    store = log["stores"][0]
    assert any(f["status"] == -1 for f in store["fetches"]), "the budget never bit"
    assert store["requests"] == len(fake.requests) == 3


def test_the_posture_no_longer_claims_that_nothing_is_ever_re_requested(
    tmp_path: Path, net: Any
) -> None:
    """``retries: "none — 403/404/429 is a recorded outcome, not something to retry around"``
    was asserted verbatim by the corpus gates while ``--resume`` re-requested exactly the 429
    and 5xx hosts on every subsequent run. Two records on the broad corpus's scratch directory
    carry ``complete: false`` for that reason. The posture now states both halves separately."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw, out = tmp_path / "raw", tmp_path / "out"
    net({"burrow.com": Storefront(products=2)})
    crc.main(
        [
            "all",
            "--raw-dir",
            str(raw),
            "--out",
            str(out),
            "--hosts-file",
            str(roster),
            "--no-compress",
        ]
    )
    retries = json.loads((out / "collection.json").read_text(encoding="utf-8"))["politeness"][
        "retries"
    ]
    assert "no retry loop within a run" in retries
    assert "--resume re-walks" in retries
    assert retries != "none — 403/404/429 is a recorded outcome, not something to retry around"


def test_the_candidate_roster_does_not_claim_to_be_uncollected() -> None:
    """Prose that outlived its subject, gated by name.

    ``candidate-hosts.txt`` said "This is a CANDIDATE list, not a collected corpus. Nothing here
    has been fetched." after all 43 of its hosts had been fetched into
    ``fixtures/real-catalogs-broad``. A roster file is the record of which businesses this
    crawler is allowed to touch, and a record that denies the traffic it authorised is worse
    than no record.
    """
    lowered = CANDIDATE_HOSTS.read_text(encoding="utf-8").lower()
    claim = "nothing here has been fetched"
    at = lowered.find(claim)
    while at >= 0:
        # The sentence may appear as a QUOTATION of the thing that was wrong — the file repeats
        # it in order to correct it, which is more use to the next reader than deleting it.
        assert lowered[at - 1 : at] == '"', (
            "the candidate roster still asserts that nothing in it was ever fetched"
        )
        at = lowered.find(claim, at + len(claim))
    assert "status: collected" in lowered, "the roster does not say what happened to it"
    assert "real-catalogs-broad" in lowered, "the roster does not point at what it produced"


def test_the_candidate_roster_agrees_with_itself_about_the_control_count() -> None:
    """The file said "Two of these are on the roster on purpose" in its glossary while carrying
    three ``off_platform_control`` rows and closing with "All three were MEASURED". A roster
    that contradicts itself about how many deliberate misses it has cannot be used to tell a
    deliberate miss from a failure, which is the only reason the role exists."""
    roster = crc.parse_hosts_file(CANDIDATE_HOSTS)
    controls = [spec for spec in roster if spec.role == "off_platform_control"]
    text = CANDIDATE_HOSTS.read_text(encoding="utf-8").lower()
    assert len(controls) == 3, [spec.host for spec in controls]
    assert "two of these are on the roster on purpose" not in text
    assert "(there are three)" in text
    assert "all three" in text


def test_a_failed_walks_requests_are_charged_to_the_record_that_survives(
    tmp_path: Path, net: Any, blocked_net: Any
) -> None:
    """A refused walk still cost the merchant a request, and the record that survives is the
    only place that number can live: the failed walk leaves no ``fetches`` rows in the corpus,
    so an accounting that reads ``fetches`` alone under-reports precisely the runs that went
    wrong."""
    roster = hosts_file(tmp_path, "roster.txt", "burrow.com furniture\n")
    raw = tmp_path / "raw"
    first = net({"burrow.com": Storefront(products=3)})
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster)))
    assert len(first.requests) == 2

    blocked = blocked_net()
    crc.run_fetch(args_for(raw, "--hosts-file", str(roster), "--no-resume"))
    assert len(blocked.requests) == 1, "the blocked walk got as far as robots.txt"

    record = json.loads((raw / "burrow.com" / crc.STORE_RECORD).read_text(encoding="utf-8"))
    assert record["walk_outcome"] == "exhausted", "the good record must still be the one on disk"
    assert record["entry"]["requests_charged"] == 3, (
        "two requests for the walk that worked plus one for the walk that did not"
    )
