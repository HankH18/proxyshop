"""T-158 — one auction, one minted discount code, **across a process boundary**.

What was measured before the fix
--------------------------------

``POST /auctions/{auction_id}/accept`` read the auction, checked ``accepted_bid_ref`` was
unset, called the merchant's ``POST /codes`` — which mints a REAL single-use discount — and
only then wrote the acceptance back. Nothing durable and nothing serialised sat between the
read and the write. Driven through the real route, 9 runs out of 9 minted two live codes, and
with a 2 ms store round trip (which is what
:class:`~exchange.auction.state.RedisAuctionStore` actually is — two separate network calls)
3 runs out of 3 answered ``HTTP 200`` twice::

    req1: HTTP 200  code PSX-LIVE-0001
    req2: HTTP 200  code PSX-LIVE-0002
    merchant POST /codes calls : 2

Why this file exists at all, given the other accept tests
----------------------------------------------------------

Because an in-process double **cannot tell process memory from durable state**. A guard built
on a ``dict`` behind a ``threading.Lock`` passes every threaded test in the repository and
still mints twice the moment the exchange runs on a second uvicorn worker — which is the
deployment. So the headline test here
(:func:`test_two_os_processes_accepting_one_auction_mint_exactly_one_code`) runs two real OS
processes, each serving the real route, against a store they share through the filesystem.
That is the smallest harness in which "process-local" and "durable" give different answers.

The control (:func:`test_the_cross_process_harness_reproduces_the_double_spend_when_disarmed`)
is not decoration either. Two processes that never overlap would make the headline test pass
on a harness that races nothing, and the file would prove nothing at all. The control wires a
claim table that always says "you won" — the pre-T-158 guard, exactly — and REQUIRES the
double spend to be observed. If it stops reproducing, the harness is broken, not fixed.

The remaining tests pin the pieces the cross-process test exercises as a bundle: the
reservation primitive itself, the ``ACCEPTED`` transition's serialisation, the in-process
route under two threads with a Redis-shaped store latency, and A5's release-on-refusal.

Ticket verify::

    PROXYSHOP_WORKER=<n> pytest apps/exchange/tests/test_t158_acceptance_claim.py -q

Wall clock: 8 seconds measured, dominated by the 2 s start-instant lead each of the four
pairs of child processes is given to boot, plus a ~150 ms mint latency per race.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from exchange.accept import accept, platform_registered_domains, use_registered_domains
from exchange.accept.claims import ClaimOutcome, StoreAcceptanceClaims
from exchange.accept.routes import InMemoryAuctionBids, configure_accept
from exchange.auction.state import (
    ACCEPTED,
    CLOSED,
    AuctionRecord,
    AuctionStateMachine,
    IllegalAuctionTransition,
    InMemoryAuctionStore,
    RedisAuctionStore,
)
from exchange.checkout import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]

SELLER_DOMAIN = "store-a.example.com"
PLATFORM_DOMAINS = {"store-a": SELLER_DOMAIN, "store-b": "store-b.example.com"}
ELIGIBILITY = {"store-a": ELIGIBLE, "store-b": ELIGIBLE}

T_NOW = 1_700_000_000.0
T_FUTURE = 2_000_000_000.0

#: The merchant's round trip. Wide on purpose: it is the whole of the window in which a
#: second accept can slip past a guard taken *after* the mint, and a window narrower than the
#: scheduler's own noise would make every race here decided by luck rather than by the guard.
MINT_LATENCY_SECONDS = 0.15

#: One store round trip. ``RedisAuctionStore.load``/``save`` are two separate network calls,
#: and this is the number that made the pre-fix route answer 200 twice, 3 runs out of 3.
STORE_ROUND_TRIP_SECONDS = 0.002

#: How long a child process is given to boot before the shared start instant. Measured child
#: startup (interpreter + ``exchange.main`` + ``fastapi.testclient``) is ~0.4 s; the margin is
#: for a loaded machine. A child that is late says so in its own report rather than silently
#: firing alone — see :func:`cross_process_accept`.
CHILD_LEAD_SECONDS = 2.0

#: Attempts the disarmed control is allowed before it concludes the harness never races. The
#: control asserts the double spend was seen *at least once* across these; the guarded test
#: gets no such latitude, because a guard that is atomic is atomic on every attempt.
CONTROL_ATTEMPTS = 3

#: Repeats of the in-process two-thread race. A single run of a concurrency test is one
#: sample of a scheduler, not a measurement.
IN_PROCESS_ATTEMPTS = 3


# =====================================================================================
# Doubles
# =====================================================================================
def honest_bid(bid_ref: str = "bid-a", store_id: str = "store-a") -> dict[str, Any]:
    """A bid whose checkout URL is on the host the platform registry has for its store."""
    domain = PLATFORM_DOMAINS[store_id]
    return {
        "bid_id": bid_ref,
        "store_id": store_id,
        "store_domain": domain,
        "offer": {
            "product_ref": "product-1",
            "unit_price": 100.0,
            "total_price": 100.0,
            "checkout_url": f"https://{domain}/cart/1:1",
            "expires_at": T_FUTURE,
        },
    }


class FileBackedAuctionStore:
    """A real :class:`~exchange.auction.state.AuctionStore` whose state is a directory.

    **This is a stand-in for Redis's ``SET key value NX EX``**, in exactly the way
    ``InMemoryAuctionStore`` is a stand-in for it everywhere else in this suite. It exists for
    one reason: two OS processes can share a directory, and cannot share a ``dict``. Under
    ``InMemoryAuctionStore`` a cross-process test is a tautology — each process reserves
    against its own memory, both win, and the harness has measured nothing.

    Atomicity comes from ``os.link``, which is the POSIX primitive for "create this name or
    fail": the kernel makes it atomic, and it raises :class:`FileExistsError` rather than
    overwriting. The token is written to a private temp file *first* and linked into place
    afterwards, so the file at the reservation path is either absent or complete — a loser can
    never read a half-written token and mistake it for the winner's.

    **The property under test is not that this class is atomic.** It is that the production
    code — :func:`exchange.accept.offer.accept` and
    :meth:`~exchange.auction.state.AuctionStateMachine._transition` — *takes the reservation
    before it asks the merchant to mint*. This class only makes the difference observable; if
    it were subtly wrong, the disarmed control would still reproduce the double spend and the
    guarded test would fail loudly, which is the pair that makes either one worth reading.

    ``save`` writes through a temp file and ``os.replace`` for the same reason ``reserve``
    links: a concurrent ``load`` in the other process must see a whole record or no record,
    never half a JSON document. The bytes written are exactly ``record.to_json()``.
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    # --- paths ----------------------------------------------------------------------
    def record_path(self, auction_id: str) -> Path:
        digest = sha256(str(auction_id).encode("utf-8")).hexdigest()
        return self._dir / f"record-{digest}.json"

    def reservation_path(self, auction_id: str, name: str) -> Path:
        digest = sha256(f"{auction_id}\x00{name}".encode()).hexdigest()
        return self._dir / f"reserved-{digest}"

    # --- the record -----------------------------------------------------------------
    def load(self, auction_id: str) -> AuctionRecord | None:
        try:
            blob = self.record_path(auction_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return AuctionRecord.from_json(blob)

    def save(self, record: AuctionRecord) -> None:
        target = self.record_path(record.auction_id)
        tmp = self._dir / f".record-{uuid.uuid4().hex}"
        tmp.write_text(record.to_json(), encoding="utf-8")
        os.replace(tmp, target)

    # --- the constraint -------------------------------------------------------------
    def reserve(self, auction_id: str, name: str, token: str) -> str | None:
        """``os.link`` as ``SET NX``: return ``None`` if we won, else the winner's token."""
        target = self.reservation_path(str(auction_id), str(name))
        tmp = self._dir / f".reserve-{uuid.uuid4().hex}"
        tmp.write_text(str(token), encoding="utf-8")
        try:
            os.link(tmp, target)
        except FileExistsError:
            return target.read_text(encoding="utf-8")
        finally:
            tmp.unlink(missing_ok=True)
        return None

    def release(self, auction_id: str, name: str, token: str) -> None:
        """Unlink only what this caller still holds — never somebody else's reservation."""
        target = self.reservation_path(str(auction_id), str(name))
        try:
            held = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        if held == str(token):
            target.unlink(missing_ok=True)


class FileBackedMerchant:
    """The merchant ``POST /codes`` client, minting across processes into a shared directory.

    A code number is allocated with ``os.open(..., O_CREAT | O_EXCL)`` on ``code-0001``,
    ``code-0002``, ... so two processes can never be issued the same number, and every code
    actually issued is appended to one shared log. The log is the evidence: a discount code the
    merchant has issued is live in the seller's account whatever the exchange does next, so
    "how many codes exist" is a question about this file and not about the HTTP statuses.

    The latency is deliberate and it is where the exposure lives — the pre-fix route did its
    ``POST /codes`` inside this window with no durable constraint held.
    """

    def __init__(
        self, directory: str | os.PathLike[str], *, latency: float = MINT_LATENCY_SECONDS
    ) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._latency = latency

    @property
    def log_path(self) -> Path:
        return self._dir / "issued-codes.log"

    def _allocate(self) -> str:
        for number in range(1, 1000):
            candidate = self._dir / f"code-{number:04d}"
            try:
                handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(handle)
            return f"PSX-LIVE-{number:04d}"
        raise RuntimeError("the merchant double ran out of code numbers")

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        time.sleep(self._latency)  # the wire, not a test sleep: see the class docstring
        code = self._allocate()
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{code} {store_id}\n")
        base = str(dict(offer).get("checkout_url") or f"https://{SELLER_DOMAIN}/cart/1:1")
        return {"code": code, "permalink_url": f"{base.split('?', 1)[0]}?discount={code}"}

    __call__ = create_code


def issued_codes(merchant_dir: str | os.PathLike[str]) -> list[str]:
    """Every code :class:`FileBackedMerchant` actually issued, from the shared log."""
    log = Path(merchant_dir) / "issued-codes.log"
    if not log.exists():
        return []
    return [line.split(" ", 1)[0] for line in log.read_text(encoding="utf-8").splitlines() if line]


class AlwaysWinsClaims:
    """The guard, disarmed: every caller is told it won. This is the pre-T-158 behaviour.

    Wired only by the control, whose whole job is to show the harness can still observe the
    double spend it was built to catch.
    """

    def claim(self, auction_id: str, bid_ref: str) -> ClaimOutcome:
        return ClaimOutcome(won=True, holder=str(bid_ref))

    def release(self, auction_id: str, bid_ref: str) -> None:
        return None


class LatentCodeCreator:
    """The in-process merchant, with a real round trip's latency, counting what it minted.

    Every entry in :attr:`calls` is a live single-use discount: the merchant has no way to
    un-issue one, so being *asked* is being *minted* as far as the seller's account is
    concerned. The permalink is built on the offer's own host, as
    ``test_accept.py``'s creator does, so no host assertion elsewhere becomes unfalsifiable.
    """

    def __init__(self, *, latency: float = MINT_LATENCY_SECONDS) -> None:
        self._latency = latency
        self._lock = threading.Lock()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        with self._lock:
            self.calls.append((str(store_id), dict(offer)))
            number = len(self.calls)
        time.sleep(self._latency)
        code = f"PSX-LIVE-{number:04d}"
        base = str(dict(offer).get("checkout_url") or f"https://{SELLER_DOMAIN}/cart/1:1")
        return {"code": code, "permalink_url": f"{base.split('?', 1)[0]}?discount={code}"}

    __call__ = create_code


class RefusingMerchant(LatentCodeCreator):
    """A merchant whose ``POST /codes`` is down for named stores. No latency: nothing races.

    The shape is ``test_accept.py``'s ``RecordingCodeCreator(explode_for=...)`` — the call is
    recorded, then it raises — kept identical so the refusal path exercised here is the one
    the rest of the suite already pins.
    """

    def __init__(self, *, explode_for: tuple[str, ...] = ()) -> None:
        super().__init__(latency=0.0)
        self.explode_for = set(explode_for)

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        if str(store_id) in self.explode_for:
            with self._lock:
                self.calls.append((str(store_id), dict(offer)))
            raise RuntimeError(f"merchant POST /codes is down for {store_id}")
        return super().create_code(store_id, offer)

    __call__ = create_code


class SlowAuctionStore:
    """``InMemoryAuctionStore`` with :class:`RedisAuctionStore`'s two round trips modelled.

    The 2 ms is on ``load`` and ``save`` only, and that is the point rather than an omission:
    the read-modify-write pair is what a second request slips between, and it is what made the
    pre-fix route answer 200 twice on 3 runs of 3. ``reserve`` is one command the server
    serialises, so a delay in front of it would widen nothing — there is no window inside a
    single atomic operation for a second caller to occupy.
    """

    def __init__(self, *, round_trip: float = STORE_ROUND_TRIP_SECONDS) -> None:
        self._inner = InMemoryAuctionStore()
        self._round_trip = round_trip

    def load(self, auction_id: str) -> AuctionRecord | None:
        time.sleep(self._round_trip)
        return self._inner.load(auction_id)

    def save(self, record: AuctionRecord) -> None:
        time.sleep(self._round_trip)
        self._inner.save(record)

    def reserve(self, auction_id: str, name: str, token: str) -> str | None:
        return self._inner.reserve(auction_id, name, token)

    def release(self, auction_id: str, name: str, token: str) -> None:
        self._inner.release(auction_id, name, token)


# =====================================================================================
# Shared wiring
# =====================================================================================
@pytest.fixture(autouse=True)
def restored_process_wiring() -> Iterator[None]:
    """``configure_accept`` writes a process-wide seller registry; put back what we found.

    Leaked wiring would let a test in another file pass on this file's registry, which is the
    exact failure ``test_accept_routes.py``'s ``unwired`` fixture exists to prevent.
    """
    previous = platform_registered_domains()
    try:
        yield
    finally:
        use_registered_domains(previous)


def closed_auction(store: Any, auction_id: str) -> AuctionStateMachine:
    """One auction in ``closed`` — the only state from which ``accepted`` is a legal move."""
    machine = AuctionStateMachine(store)
    machine.create(auction_id, intent_id="intent-1", cluster_id="cluster-1", roster=[])
    machine.open(auction_id, now=T_NOW)
    machine.close(auction_id, now=T_NOW + 1.0)
    return machine


def wired_app(machine: AuctionStateMachine, auction_id: str, creator: Any, **extra: Any) -> Any:
    """The served exchange, wired the way ``test_accept_routes.py::wired_app`` wires it."""
    book = InMemoryAuctionBids()
    book.record(auction_id, [honest_bid("bid-a"), honest_bid("bid-b", "store-b")])
    app = create_app()
    configure_accept(
        app,
        machine=machine,
        bids=book,
        code_creator=creator,
        checkout_mode="shopify",
        registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS),
        eligibility=StaticSellerEligibility(ELIGIBILITY),
        **extra,
    )
    return app


# =====================================================================================
# The cross-process harness
# =====================================================================================
#: The child process, in full. It loads THIS module by path rather than re-declaring the
#: store and the merchant, so the class the child reserves against is byte-for-byte the class
#: the parent pre-creates the auction with — a second copy would be free to drift into
#: passing.
CHILD_SCRIPT = '''\
"""One OS process serving one POST /auctions/{auction_id}/accept. Written by T-158's tests."""

import importlib.util
import sys


def main() -> int:
    helpers_path = sys.argv[1]
    spec = importlib.util.spec_from_file_location("t158_child_helpers", helpers_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the T-158 test helpers from " + repr(helpers_path))
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    return int(helpers.run_child(sys.argv[2:]))


if __name__ == "__main__":
    raise SystemExit(main())
'''


def run_child(argv: Sequence[str]) -> int:
    """Serve exactly one accept, at ``start_at``, and report the outcome as JSON on stdout.

    Runs in a separate OS process. Everything is built and warmed *before* the shared start
    instant so the only thing between the two processes at that instant is the request itself.
    """
    store_dir, merchant_dir, auction_id, bid_ref, start_at, guard = argv

    machine = AuctionStateMachine(FileBackedAuctionStore(store_dir))
    book = InMemoryAuctionBids()
    book.record(auction_id, [honest_bid(bid_ref)])
    app = create_app()
    configure_accept(
        app,
        machine=machine,
        bids=book,
        code_creator=FileBackedMerchant(merchant_dir),
        checkout_mode="shopify",
        registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS),
        eligibility=StaticSellerEligibility(ELIGIBILITY),
        # `None` is not "no guard": `configure_accept` then derives the claim table from the
        # machine's own store, which is the shared directory. That derivation IS the fix at
        # the wiring level, so the guarded child must exercise it rather than pass a table.
        claims=AlwaysWinsClaims() if guard == "disabled" else None,
    )

    with TestClient(app) as client:
        # Warm the ASGI stack before the clocks are compared. The first request through a
        # fresh app pays route matching and pydantic model building; paying that after the
        # start instant would put tens of milliseconds of unmodelled skew between the two
        # processes. An auction nobody created is a 404 and mints nothing.
        client.post("/auctions/auction-warmup/accept", json={"bid_ref": bid_ref})

        ready_at = time.time()
        target = float(start_at)
        slack = target - time.time() - 0.005
        if slack > 0:
            time.sleep(slack)
        while time.time() < target:  # busy-wait the last few ms: no scheduler slack
            pass
        fired_at = time.time()
        response = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": bid_ref})

    print(
        json.dumps(
            {
                "pid": os.getpid(),
                "status": response.status_code,
                "body": response.text,
                "ready_at": ready_at,
                "fired_at": fired_at,
                "late": ready_at > target,
            }
        )
    )
    return 0


@dataclass
class RaceOutcome:
    """What two racing processes did: the statuses they were given, the codes that exist."""

    statuses: list[int] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    transcript: str = ""

    @property
    def skew_ms(self) -> float:
        fired = [float(report["fired_at"]) for report in self.reports]
        return (max(fired) - min(fired)) * 1000.0 if len(fired) == 2 else float("nan")

    def summary(self) -> str:
        return (
            f"statuses={sorted(self.statuses)} codes={self.codes} "
            f"start-skew={self.skew_ms:.1f}ms\n{self.transcript}"
        )


def _child_environment() -> dict[str, str]:
    """``os.environ`` (so ``PROXYSHOP_WORKER`` reaches the child) plus the import roots.

    ``pyproject.toml``'s ``[tool.pytest.ini_options] pythonpath = [".", ".pkgroot"]`` is what
    puts ``exchange`` on the path under pytest, and a child is not running pytest. ``.pkgroot``
    is the tracked symlink tree that makes the flat ``src/`` layouts importable (R1b).
    """
    env = dict(os.environ)
    roots = [str(REPO_ROOT), str(REPO_ROOT / ".pkgroot")]
    existing = env.get("PYTHONPATH")
    if existing:
        roots.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(roots)
    return env


def cross_process_accept(
    workdir: Path, *, bid_ref: str, guard: str, lead: float = CHILD_LEAD_SECONDS
) -> RaceOutcome:
    """Two OS processes accept one auction at one instant. Returns statuses and codes minted.

    Both children get the SAME ``bid_ref``: the double-click / client retry, which is the case
    the ticket's reproduction shows is live. The auction is pre-created here, in the parent,
    through the same :class:`FileBackedAuctionStore` the children will open.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    store_dir = workdir / "store"
    merchant_dir = workdir / "merchant"
    script = workdir / "accept_once.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")

    auction_id = "auction-t158"
    machine = closed_auction(FileBackedAuctionStore(store_dir), auction_id)
    assert machine.state_of(auction_id) == CLOSED

    start_at = time.time() + lead
    argv = [
        sys.executable,
        str(script),
        str(Path(__file__).resolve()),
        str(store_dir),
        str(merchant_dir),
        auction_id,
        bid_ref,
        f"{start_at!r}",
        guard,
    ]
    processes = [
        subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_environment(),
            text=True,
        )
        for _ in range(2)
    ]

    outcome = RaceOutcome()
    chunks: list[str] = []
    for index, process in enumerate(processes):
        stdout, stderr = process.communicate(timeout=120)
        chunks.append(f"--- child {index} rc={process.returncode}\n{stdout}{stderr}")
        report: dict[str, Any] | None = None
        for line in reversed(stdout.splitlines()):
            try:
                report = json.loads(line)
            except ValueError:
                continue
            break
        assert report is not None, f"child {index} printed no JSON report:\n{stdout}{stderr}"
        outcome.reports.append(report)
        outcome.statuses.append(int(report["status"]))
    outcome.transcript = "\n".join(chunks)
    outcome.codes = issued_codes(merchant_dir)
    return outcome


# =====================================================================================
# 1-2. The headline: the guard survives a process boundary
# =====================================================================================
def test_two_os_processes_accepting_one_auction_mint_exactly_one_code(tmp_path: Path) -> None:
    """Two processes, one auction, one instant, one shared store: exactly one live code.

    This is the test the ticket is for. Every other accept test in this suite runs in one
    process, where a guard held in a ``dict`` is indistinguishable from a guard held in Redis;
    here they differ, because the two children share nothing but a directory. The pre-fix
    route minted twice in 9 runs of 9 under this shape, and a fix that put the guard *after*
    ``POST /codes`` would still leave two live single-use discounts here while answering one
    409 — which is why the code count is asserted separately from the statuses.
    """
    outcome = cross_process_accept(tmp_path / "guarded", bid_ref="bid-a", guard="claimed")

    assert sorted(outcome.statuses) == [200, 409], outcome.summary()
    assert len(outcome.codes) == 1, (
        f"the merchant issued {len(outcome.codes)} live single-use codes for one "
        f"purchase; {outcome.summary()}"
    )


def test_the_winning_process_is_the_one_that_holds_the_persisted_acceptance(
    tmp_path: Path,
) -> None:
    """The 200 and the durable record agree, and the loser's 409 names the double accept.

    A route that answered 200 without stamping the auction would leave the *next* request able
    to mint again a moment later — the same double spend, spread over two seconds instead of
    two milliseconds.
    """
    workdir = tmp_path / "persisted"
    outcome = cross_process_accept(workdir, bid_ref="bid-a", guard="claimed")
    assert sorted(outcome.statuses) == [200, 409], outcome.summary()

    record = FileBackedAuctionStore(workdir / "store").load("auction-t158")
    assert record is not None, outcome.summary()
    assert record.state == ACCEPTED, outcome.summary()
    assert record.accepted_bid_ref == "bid-a", outcome.summary()

    winner = next(r for r in outcome.reports if int(r["status"]) == 200)
    loser = next(r for r in outcome.reports if int(r["status"]) == 409)
    assert outcome.codes[0] in str(winner["body"]), outcome.summary()
    assert "already accepted" in str(loser["body"]), outcome.summary()
    assert outcome.codes[0] not in str(loser["body"]), (
        "the refusal handed the loser the winner's live discount code; " + outcome.summary()
    )


# =====================================================================================
# 3. The control — the harness is not vacuous
# =====================================================================================
def test_the_cross_process_harness_reproduces_the_double_spend_when_disarmed(
    tmp_path: Path,
) -> None:
    """Disarm the claim and the SAME harness must mint twice. Otherwise it races nothing.

    The child wires :class:`AlwaysWinsClaims` — a claim table that tells every caller it won,
    which is precisely what the pre-T-158 route had — and changes nothing else. Two live codes
    must appear in the shared merchant log.

    **Only the claim is disarmed, and the observed statuses are the reason this control is
    worth more than a plain "it mints twice".** The state machine's own ``exit:closed``
    reservation is still armed in the child, so the second process is refused its stamp and
    answers 409 — *after* ``POST /codes`` has already issued its discount. Measured here::

        child 0: HTTP 409  auction 'auction-t158' has already left 'closed' for 'accepted'
        child 1: HTTP 200  PSX-LIVE-0002
        codes actually issued: ['PSX-LIVE-0001', 'PSX-LIVE-0002']

    That is exactly the outcome ``accept/claims.py`` names as the trap: an atomic transition
    alone buys a correct HTTP status over a double spend. Which is why the assertion below is
    about the merchant's log and not about the statuses.

    It is allowed up to :data:`CONTROL_ATTEMPTS` attempts and asserts the double spend was
    seen at least once, because this direction depends on the two processes genuinely
    overlapping and the guarded direction does not: an atomic reservation is atomic on every
    attempt, an accidental serialisation is not. **If this stops reproducing, the harness is
    broken** — widen the mint latency, tighten the start sync, or add attempts. Do not weaken
    the guarded test above, which would then be passing for the wrong reason.
    """
    attempts: list[RaceOutcome] = []
    for attempt in range(CONTROL_ATTEMPTS):
        outcome = cross_process_accept(
            tmp_path / f"disarmed-{attempt}", bid_ref="bid-a", guard="disabled"
        )
        attempts.append(outcome)
        if len(outcome.codes) >= 2:
            assert 200 in outcome.statuses, (
                "both processes were refused, so nothing was minted for the right reason; "
                + outcome.summary()
            )
            return

    report = "\n\n".join(o.summary() for o in attempts)
    raise AssertionError(
        f"the disarmed guard minted once or less in {CONTROL_ATTEMPTS} attempts, so the "
        f"harness never actually raced and the guarded test above proves nothing:\n{report}"
    )


# =====================================================================================
# 4. In-process, through the real served route
# =====================================================================================
def test_two_concurrent_accepts_through_the_served_route_mint_once() -> None:
    """Two threads, one app, a Redis-shaped store latency: one 200 and one mint, every time.

    The store gives ``load`` and ``save`` a 2 ms round trip because that is what
    ``RedisAuctionStore`` is — two separate network calls — and it is the configuration under
    which the pre-fix route answered ``HTTP 200`` twice on 3 runs out of 3. Both requests are
    released from one barrier after their clients are up, so the window is the merchant's
    150 ms mint rather than whatever the scheduler happened to do.
    """
    for attempt in range(IN_PROCESS_ATTEMPTS):
        auction_id = f"auction-threads-{attempt}"
        machine = closed_auction(SlowAuctionStore(), auction_id)
        merchant = LatentCodeCreator()
        app = wired_app(machine, auction_id, merchant)
        barrier = threading.Barrier(2)

        def fire(bid_ref: str, app: Any = app, barrier: Any = barrier) -> int:
            with TestClient(app) as client:
                barrier.wait(timeout=30)
                return int(
                    client.post(
                        f"/auctions/{auction_id}/accept", json={"bid_ref": bid_ref}
                    ).status_code
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(
                f.result() for f in [pool.submit(fire, "bid-a"), pool.submit(fire, "bid-b")]
            )

        assert statuses == [200, 409], f"attempt {attempt}: statuses={statuses}"
        assert len(merchant.calls) == 1, (
            f"attempt {attempt}: the merchant was asked to mint {len(merchant.calls)} times "
            f"for one auction; statuses={statuses}"
        )
        record = machine.store.load(auction_id)
        assert record is not None and record.state == ACCEPTED
        assert record.accepted_bid_ref in ("bid-a", "bid-b")


# =====================================================================================
# 5. The reservation primitive
# =====================================================================================
def test_a_reservation_is_won_exactly_once_and_names_its_holder_afterwards() -> None:
    """``reserve`` returning ``None`` is the *only* way to be told you won."""
    store = InMemoryAuctionStore()

    assert store.reserve("auction-1", "accept", "bid-a") is None
    assert store.reserve("auction-1", "accept", "bid-b") == "bid-a"
    assert store.reserve("auction-1", "accept", "bid-a") == "bid-a", (
        "re-reserving with the winner's own token was treated as a fresh win"
    )
    # A reservation constrains one (auction, name) pair and nothing else: a different move on
    # the same auction, and the same move on a different auction, are both still open.
    assert store.reserve("auction-1", "exit:closed", ACCEPTED) is None
    assert store.reserve("auction-2", "accept", "bid-b") is None


def test_releasing_a_reservation_somebody_else_holds_is_a_no_op() -> None:
    """This is how a "safe" retry mints the second code, so it must not be possible."""
    store = InMemoryAuctionStore()
    assert store.reserve("auction-1", "accept", "bid-a") is None

    store.release("auction-1", "accept", "bid-b")

    assert store.reserve("auction-1", "accept", "bid-c") == "bid-a", (
        "a caller released a claim it never held and the auction reopened for minting"
    )


def test_releasing_a_reservation_you_hold_frees_it_for_the_next_attempt() -> None:
    """A5: a refused accept re-offers the next slot, which needs the claim given back."""
    store = InMemoryAuctionStore()
    assert store.reserve("auction-1", "accept", "bid-a") is None

    store.release("auction-1", "accept", "bid-a")

    assert store.reserve("auction-1", "accept", "bid-b") is None


def test_many_threads_racing_one_reservation_produce_exactly_one_winner() -> None:
    """32 threads, one key, one winner — and every loser is told who won, not merely refused."""
    store = InMemoryAuctionStore()
    threads = 32
    barrier = threading.Barrier(threads)

    def attempt(index: int) -> str | None:
        barrier.wait(timeout=30)
        return store.reserve("auction-1", "accept", f"bid-{index}")

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(attempt, range(threads)))

    winners = [r for r in results if r is None]
    assert len(winners) == 1, f"{len(winners)} threads were told they won: {results}"
    holders = {r for r in results if r is not None}
    assert len(holders) == 1, f"the losers were told different winners: {holders}"


# =====================================================================================
# 6. The ACCEPTED transition
# =====================================================================================
def test_the_accepted_transition_is_serialised_across_threads() -> None:
    """Two accepts, different bids, one closed auction: one wins, one raises, one stamp.

    Before T-158 this measured ``2 of 2`` succeeded, with the second ``save`` silently
    overwriting the first — both callers were told their bid had been accepted, and the ledger
    carried two ``accepted`` events for one auction. The 2 ms store round trip guarantees both
    threads read ``closed`` before either reserves, so the reservation is what decides it.
    """
    auction_id = "auction-transition"
    machine = closed_auction(SlowAuctionStore(), auction_id)
    barrier = threading.Barrier(2)

    def attempt(bid_ref: str) -> tuple[str, str]:
        barrier.wait(timeout=30)
        try:
            machine.accept(auction_id, bid_ref, now=T_NOW + 2.0)
        except IllegalAuctionTransition as exc:
            return ("refused", str(exc))
        return ("accepted", bid_ref)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            f.result() for f in [pool.submit(attempt, "bid-a"), pool.submit(attempt, "bid-b")]
        ]

    accepted = [ref for kind, ref in outcomes if kind == "accepted"]
    refused = [reason for kind, reason in outcomes if kind == "refused"]
    assert len(accepted) == 1, f"{len(accepted)} threads accepted one auction: {outcomes}"
    assert len(refused) == 1, f"outcomes={outcomes}"

    record = machine.store.load(auction_id)
    assert record is not None
    assert record.state == ACCEPTED
    assert record.accepted_bid_ref == accepted[0], (
        f"the persisted stamp is {record.accepted_bid_ref!r} but the caller told it won was "
        f"{accepted[0]!r}; outcomes={outcomes}"
    )
    assert [entry["state"] for entry in record.history].count(ACCEPTED) == 1, record.history


# =====================================================================================
# 7. A5 — a refused mint gives the claim back
# =====================================================================================
def test_a_checkout_that_is_refused_releases_the_claim_for_the_next_slot() -> None:
    """A5: a refusal re-offers the next slot, so the auction must still be claimable.

    A claim kept after a failed mint would be worse than the bug it replaced: one unreachable
    merchant would wedge the auction for its whole 15-minute TTL, and the buyer would get
    neither this slot nor the next one. The refusing merchant is ``test_accept.py``'s shape —
    the call is recorded, then ``POST /codes`` raises.
    """
    claims = StoreAcceptanceClaims(InMemoryAuctionStore())
    live: dict[str, Any] = {
        "auction_id": "auction-a5",
        "bids": [honest_bid("bid-a"), honest_bid("bid-b", "store-b")],
        "accepted_bid_ref": None,
        "now": T_NOW,
    }
    sellers = StaticRegisteredDomains(PLATFORM_DOMAINS)

    down = RefusingMerchant(explode_for=("store-a",))
    refused = accept(live, "bid-a", down, "shopify", registered_domains=sellers, claims=claims)
    assert refused.accepted is False, refused.denial_reason
    assert live["accepted_bid_ref"] is None, "a refused accept stamped the auction"

    # The claim is back: the next slot can be offered, and it mints.
    up = LatentCodeCreator(latency=0.0)
    second = accept(live, "bid-b", up, "shopify", registered_domains=sellers, claims=claims)
    assert second.accepted is True, second.denial_reason
    assert len(up.calls) == 1
    assert live["accepted_bid_ref"] == "bid-b"

    # And the successful accept KEEPS its claim — that is what "one auction, one code" means.
    assert claims.claim("auction-a5", "bid-a") == ClaimOutcome(won=False, holder="bid-b")


# =====================================================================================
# 8. The Redis path — the store the deployment actually runs on
# =====================================================================================
@pytest.mark.docker
def test_the_redis_store_reserves_atomically_across_independent_instances(
    redis_client: Any,
) -> None:
    """``SET key token NX EX`` from two independent stores: one winner, and the loser is told.

    The two :class:`RedisAuctionStore` instances stand in for two uvicorn workers: they share
    no Python state, only the server, which is the entire reason the DESIGN-pinned store is
    Redis and not a dictionary. Skips with an explicit reason when the compose stack is down —
    the ``redis_client`` fixture decides that, per service (T-109).
    """
    first = RedisAuctionStore(redis_client)
    second = RedisAuctionStore(redis_client)
    auction_id = f"auction-redis-{uuid.uuid4().hex[:8]}"

    assert first.reserve(auction_id, "accept", "bid-a") is None
    assert second.reserve(auction_id, "accept", "bid-b") == "bid-a", (
        "a second process was told it won an acceptance the first already held"
    )

    # A release from the wrong holder must not reopen the auction for a second mint.
    second.release(auction_id, "accept", "bid-b")
    assert second.reserve(auction_id, "accept", "bid-c") == "bid-a"

    # The holder can give it back, which is what A5's re-offer needs.
    first.release(auction_id, "accept", "bid-a")
    assert second.reserve(auction_id, "accept", "bid-c") is None

    # The reservation lives beside the record, under the auction's own key prefix and TTL.
    assert RedisAuctionStore.reservation_key(auction_id, "accept").startswith(
        f"auction:{auction_id}:"
    )
    assert redis_client.ttl(RedisAuctionStore.reservation_key(auction_id, "accept")) > 0
