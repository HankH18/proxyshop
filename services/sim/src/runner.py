"""T-081 — the headless market simulation (SPEC S1 / S4 / C9).

One seed, one script, and the whole network moves: the approved fixture intent goes out to
the approved store roster, the R12 eligibility gate decides who is even asked, the exchange
collects bids and enforces its own deadline, a winner is accepted through the real checkout
port with real exact-host domain validation, the resulting ledger events go into the real
hash-chained event store, and the dishonest store's scripted behaviours land on the real
trust engine — which is then given the chance to catch it inside the approved episode
budget.

Nothing here is a stand-in for a system this repository already has. Every step below calls
the shipping module through its public entry point, because the entire value of a simulator
is that it exercises the code that will actually run:

===================================  ==============================================
step                                 module driven
===================================  ==============================================
who may be asked at all              ``exchange.eligibility`` (R12)
who is asked, and by when            ``exchange.orchestration.solicit_bids`` (R10)
what a late or silent store costs    ``exchange.auction.collect_bids``
the auction's own lifecycle          ``exchange.auction.AuctionStateMachine``
accepting an offer                   ``exchange.accept.accept`` (D22/C10 host check)
what the adversary pitches           ``seller_reference.personas`` (A3)
the tamper-evident record            ``trust.events.InMemoryEventStore``
whether the adversary is caught      ``trust.scoring`` / ``trust.snapshot``
the adversary's script               :mod:`sim.dishonest` (A3)
the catalog and roster               ``fixtures.generator`` (T-080)
===================================  ==============================================

Determinism, and the one thing that is deliberately NOT deterministic
---------------------------------------------------------------------
T-081 acceptance 1 is "a fixed seed reproduces identical event streams twice", and
:func:`run_simulation` satisfies it — with one carve-out that is a property of the platform
being correct rather than of this simulator giving up.

``exchange.checkout.codes.mint_code`` draws from :mod:`secrets`, so a discount code is not
derivable from the offer, the auction, or anything else an outsider can see (D22). The
checkout token is minted the same way, and ``build_event`` stamps a fresh UUID and a
wall-clock instant on every ledger event. A simulator that demanded byte-identical codes
across runs would be demanding a *guessable* discount code — it would be asserting a
security defect. So :func:`normalise_event` masks exactly those four fields, by name, and
the run separately asserts that the real values are well-formed and never repeat
(:func:`minted_codes`). Everything else in the stream — kinds, order, store, auction,
prices, offers, expiries — is compared byte for byte.

The clock is injected, never read. Episodes are one simulated day apart, counted from the
instant the human approved the manifest (``approval.approved_at``), because the approved
trust trajectory is stated in days and decay is measured from each observation's own date.
No ``time.time()``, no ``datetime.now()``, no sleep, no socket, no database: the whole run
is a pure function of ``(manifest, seed)``.

Bounded by construction (acceptance 3)
---------------------------------------
The run is ``episode_budget`` episodes long and each episode asks each rostered store at
most once. The budget is the human's, read from the manifest; ``episodes=`` may shorten a
run but never extend it past what was approved.
"""

from __future__ import annotations

__all__ = [
    "EPISODE_SECONDS",
    "Episode",
    "SimulationError",
    "SimulationRun",
    "build_roster",
    "episode_instant",
    "honest_observations",
    "ledger_contract_problems",
    "minted_codes",
    "normalise_event",
    "run_simulation",
    "trust_observations",
]

import hashlib
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .dishonest import FIRST_OBSERVED_EPISODE, run_dishonest_campaign, scripted_behaviours

#: One episode per simulated day — the replay schedule the approved manifest states, and the
#: only schedule under which its published trust trajectory means anything.
EPISODE_SECONDS = 86_400.0

#: How long the bidding window lasts, in the exchange's own frame. A store that has not
#: answered inside it falls back to list price exactly as if it had stayed silent (R10).
BID_WINDOW_SECONDS = 3.0

#: Payload fields the platform mints as secrets or stamps from a wall clock. Masked in the
#: canonical stream — see the module docstring: a reproducible discount code is a guessable
#: discount code. Their real values are checked for shape and uniqueness instead.
MINTED_PAYLOAD_FIELDS = frozenset({"checkout_token", "discount_code", "code", "permalink_url"})

#: What a masked field is replaced by. A single constant so a diff of two runs shows the
#: masking, rather than showing two different secrets and calling the run nondeterministic.
MASKED = "<minted>"

#: The store the manifest describes as "honest but slow". It is on the roster and it is
#: eligible; it simply does not answer inside the window, which is what exercises R10's
#: timeout without anybody being dishonest about it.
_SLOW_ROLE_MARKER = "slow"


class SimulationError(RuntimeError):
    """The simulation cannot be run against this manifest or this repository state."""


@dataclass(frozen=True)
class Episode:
    """One simulated day: one auction, from solicitation to acceptance."""

    episode: int
    auction_id: str
    solicited: tuple[str, ...]
    denied: tuple[tuple[str, str], ...]
    bidders: tuple[str, ...]
    fallbacks: tuple[tuple[str, str], ...]
    winner: str | None
    accepted: bool
    denial_reason: str | None
    ledger_kinds: tuple[str, ...]
    score: float
    blacklisted: bool

    def to_json(self) -> dict[str, Any]:
        """Plain JSON data, for byte-identity comparison between runs."""
        return {
            "episode": self.episode,
            "auction_id": self.auction_id,
            "solicited": list(self.solicited),
            "denied": [list(pair) for pair in self.denied],
            "bidders": list(self.bidders),
            "fallbacks": [list(pair) for pair in self.fallbacks],
            "winner": self.winner,
            "accepted": self.accepted,
            "denial_reason": self.denial_reason,
            "ledger_kinds": list(self.ledger_kinds),
            "score": self.score,
            "blacklisted": self.blacklisted,
        }


@dataclass(frozen=True)
class SimulationRun:
    """Everything one seeded run produced, as plain data."""

    seed: int
    seed_category: str
    episodes: tuple[Episode, ...]
    events: tuple[dict[str, Any], ...]
    observations: tuple[dict[str, Any], ...]
    codes: tuple[str, ...]
    head_hash: str
    chain_ok: bool
    caught_at: int | None
    ledger_contract_problems: tuple[tuple[str, str], ...] = ()
    snapshot: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """The canonical, comparable form of a run. This is the "event stream" of S4/C9."""
        return {
            "seed": self.seed,
            "seed_category": self.seed_category,
            "episodes": [episode.to_json() for episode in self.episodes],
            "events": [dict(event) for event in self.events],
            "observations": [dict(observation) for observation in self.observations],
            "chain_ok": self.chain_ok,
            "caught_at": self.caught_at,
            "ledger_contract_problems": [list(pair) for pair in self.ledger_contract_problems],
        }


# --- the approved inputs -------------------------------------------------------------
def _manifest_str(manifest: Mapping[str, Any], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SimulationError(f"manifest.{key} must be a non-empty string")
    return value


def _manifest_int(manifest: Mapping[str, Any], key: str) -> int:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SimulationError(f"manifest.{key} must be an integer")
    return value


def _base_instant(manifest: Mapping[str, Any]) -> datetime:
    """Day zero: the instant the human approved the script being replayed.

    Not ``now``. The approved trust trajectory is a statement about elapsed days between
    observations, so anchoring the run to a wall clock would make two runs of the same seed
    disagree — and would make the trajectory unreproducible a month from now.
    """
    approval = manifest.get("approval")
    stamp = approval.get("approved_at") if isinstance(approval, Mapping) else None
    if not isinstance(stamp, str) or not stamp.strip():
        raise SimulationError(
            "manifest.approval.approved_at must be the ISO-8601 instant the manifest was "
            "approved; the simulation counts its episode days from it rather than from a clock"
        )
    return datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))


def episode_instant(manifest: Mapping[str, Any], episode: int) -> str:
    """The ISO-8601 instant of one episode — one simulated day per episode."""
    day = max(0, int(episode) - FIRST_OBSERVED_EPISODE)
    moment = _base_instant(manifest) + timedelta(days=day)
    return moment.isoformat().replace("+00:00", "Z")


def _episode_epoch(manifest: Mapping[str, Any], episode: int) -> float:
    """The same instant as a float epoch, which is the exchange's clock frame."""
    day = max(0, int(episode) - FIRST_OBSERVED_EPISODE)
    return (_base_instant(manifest) + timedelta(days=day)).timestamp()


def build_roster(payload: Mapping[str, Any], manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The auction roster: one row per approved store, priced from the seeded catalog.

    The stores are the manifest's (which store is dishonest is ground truth a human
    approves, not a catalog knob) and the prices are T-080's generator's, so the roster is
    the product of the two documents rather than of this module.
    """
    stores = manifest.get("stores")
    if not isinstance(stores, Sequence) or not stores:
        raise SimulationError("manifest.stores must list the approved store roster")

    prices: dict[str, tuple[str, float]] = {}
    for product in payload.get("catalog") or []:
        store_id = str(product.get("store_id"))
        if store_id in prices:
            continue
        variants = product.get("variants") or []
        if not variants:
            continue
        prices[store_id] = (str(product["product_ref"]), float(variants[0]["price"]))

    roster: list[dict[str, Any]] = []
    for store in stores:
        store_id = str(store["store_id"])
        if store_id not in prices:
            raise SimulationError(
                f"the seeded catalog carries no product for approved store {store_id!r}; "
                "the roster cannot be priced from a catalog that does not cover it"
            )
        product_ref, list_price = prices[store_id]
        roster.append(
            {
                "store_id": store_id,
                "tier": 1,
                "product_ref": product_ref,
                "list_price": list_price,
                "store_domain": str(store.get("domain") or f"{store_id}.example.com"),
                "honest": bool(store.get("honest", True)),
                "role": str(store.get("role") or ""),
                "business_identity": str(store.get("business_identity") or store_id),
            }
        )
    return roster


# --- the bidders ---------------------------------------------------------------------
class _ScriptedSolicitor:
    """The outbound bid-request client, answering from the approved persona scripts.

    Honest stores quote their own catalog price less a seeded, modest introductory
    discount. The dishonest store quotes the price the **approved aggressive persona**
    pitches — which the manifest records as below what checkout will really charge — so the
    lie the simulation tells is the lie a human signed off, not one this module invented.

    The store the manifest marks "honest but slow" answers ``None``: it is eligible, it is
    asked, and it simply does not reply inside the window. ``collect_bids`` represents it at
    list price with the reason ``no_response``, which is R10's timeout doing its job without
    anyone being dishonest.
    """

    def __init__(
        self,
        *,
        auction_id: str,
        pitched_prices: Mapping[str, float],
        silent: frozenset[str],
        expires_at: float,
    ) -> None:
        self._auction_id = auction_id
        self._prices = pitched_prices
        self._silent = silent
        self._expires_at = expires_at

    def solicit(self, store: Mapping[str, Any]) -> Mapping[str, Any] | None:
        store_id = str(store["store_id"])
        if store_id in self._silent:
            return None
        price = self._prices.get(store_id)
        if price is None:
            return None
        return {
            "store_id": store_id,
            "bid": {
                "auction_id": self._auction_id,
                "store_id": store_id,
                "offer": {
                    "product_ref": str(store["product_ref"]),
                    "unit_price": price,
                    "total_price": price,
                    "currency": "USD",
                    "checkout_url": (
                        f"https://{store['store_domain']}/cart/1:1?ref={self._auction_id}"
                    ),
                    "expires_at": self._expires_at,
                },
                "claims": [],
            },
        }


def _persona_unit_price(manifest: Mapping[str, Any], persona: str) -> float | None:
    """The unit price the named approved persona pitches, or ``None`` if it pitches none."""
    personas = manifest.get("personas")
    if not isinstance(personas, Mapping):
        return None
    entry = personas.get(persona)
    if not isinstance(entry, Mapping):
        return None
    for claim in entry.get("scripted_claims") or []:
        if isinstance(claim, Mapping) and claim.get("claim_type") == "unit_price":
            try:
                return float(str(claim.get("value")))
            except (TypeError, ValueError):
                return None
    return None


def _pitched_prices(
    manifest: Mapping[str, Any], roster: Sequence[Mapping[str, Any]], seed: int, episode: int
) -> dict[str, float]:
    """What each store quotes this episode."""
    dishonest_id = str((manifest.get("dishonest_store") or {}).get("store_id") or "")
    pitched = _persona_unit_price(manifest, "aggressive")
    out: dict[str, float] = {}
    for row in roster:
        store_id = str(row["store_id"])
        if store_id == dishonest_id and pitched is not None:
            # The approved lie: a headline price below what checkout will charge.
            out[store_id] = pitched
            continue
        rng = _rng(seed, "quote", episode, store_id)
        out[store_id] = round(float(row["list_price"]) * (1.0 - rng.uniform(0.0, 0.08)), 2)
    return out


def _rng(seed: int, *parts: object) -> random.Random:
    """The same sha256-derived stream :mod:`sim.dishonest` uses. Never ``Random(tuple)``."""
    material = ":".join(str(part) for part in (seed, *parts))
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# --- the record ----------------------------------------------------------------------
def normalise_event(event: Mapping[str, Any], *, episode: int, sequence: int) -> dict[str, Any]:
    """One ledger event, reduced to the part two runs of the same seed must agree on.

    ``event_id`` and ``ts`` are dropped (a UUID and a wall-clock stamp) and the four minted
    payload fields are masked. Everything else survives verbatim — including the offer, the
    prices and the expiry, which are all functions of the seed.
    """
    payload = event.get("payload")
    reduced: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        for key in sorted(payload):
            reduced[key] = MASKED if key in MINTED_PAYLOAD_FIELDS else payload[key]
    return {
        "episode": episode,
        "sequence": sequence,
        "kind": event.get("kind"),
        "auction_id": event.get("auction_id"),
        "store_id": event.get("store_id"),
        "order_ref": event.get("order_ref"),
        "payload": reduced,
    }


def minted_codes(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Every discount code the run really minted, in order, unmasked.

    Read off ``code_created`` only — the kind whose job is to *record* the mint. The same
    code is repeated on the accompanying ``checkout_redirect``, so scanning every event
    counts each mint twice and would turn "codes never repeat" into a test that can only
    fail. Both spellings are accepted because the exchange currently writes
    ``discount_code`` here while ``contracts.ledger`` publishes ``code``; see the
    conformance report :func:`ledger_contract_problems` builds.
    """
    codes: list[str] = []
    for event in events:
        if str(event.get("kind")) != "code_created":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        for key in ("code", "discount_code"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                codes.append(value)
                break
    return codes


def ledger_contract_problems(events: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Every published-payload-shape violation in a run's ledger stream.

    ``contracts.ledger.validate_ledger_payload`` is the frozen statement of what each
    ``LedgerEvent`` kind's payload must carry (D24), and nothing on the accept path calls
    it — so a producer can drift from the published body and no test notices. Driving the
    real accept path and then checking its output against the real contract is exactly the
    kind of cross-system disagreement a simulator exists to find, so the run carries the
    report rather than swallowing it.
    """
    from contracts.ledger import validate_ledger_payload

    problems: list[tuple[str, str]] = []
    for event in events:
        kind = str(event.get("kind"))
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        for problem in validate_ledger_payload(kind, payload):
            entry = (kind, str(problem))
            if entry not in problems:
                problems.append(entry)
    return problems


def honest_observations(
    manifest: Mapping[str, Any], store_id: str, episode: int
) -> list[dict[str, Any]]:
    """The positive mirror of the approved dishonest script, for a store that delivered.

    The *dimensions* come from the manifest's own behaviour list rather than from a second
    list here, for the same reason the dishonest script does: an honest control whose
    dimensions were chosen by the simulator would be graded on a different surface from the
    adversary, and "the honest store stays above the threshold" would stop being a
    comparison. The positive observation type is the engine's own vocabulary — a product
    fact is ``verified``, a transaction promise is ``fulfilled``.

    This control matters. Without it, S2's claim is satisfiable by a scorer that sinks
    everybody, which is precisely what the approved manifest says the honest persona exists
    to rule out.
    """
    from trust.scoring import CATALOG_DIMENSION

    observed_at = episode_instant(manifest, episode)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for behaviour in scripted_behaviours(manifest):
        dim = str(behaviour["dim"])
        if dim in seen:
            continue
        seen.add(dim)
        out.append(
            {
                "store_id": store_id,
                "dim": dim,
                "type": "verified" if dim == CATALOG_DIMENSION else "fulfilled",
                "observed_at": observed_at,
                "episode": episode,
            }
        )
    return out


def trust_observations(
    records: Iterable[Mapping[str, Any]], manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Turn emitted behaviour records into observations ``trust.scoring.score`` accepts.

    Deliberately does **not** forward the record's ``published_weight``. The trust engine's
    observation ``weight`` field is a *relative* multiplier in ``[0, 1]`` (a buyer's track
    record, R14) — not the published absolute weight for the observation type, which the
    engine looks up itself from ``type``. Passing 2.0 there raises
    ``InvalidObservationWeight``, and passing it silently rescaled would be the simulator
    re-weighting its own adversary. The two are different numbers with the same short name;
    they are kept apart here on purpose.
    """
    out: list[dict[str, Any]] = []
    for record in records:
        out.append(
            {
                "store_id": record["store_id"],
                "dim": record["dim"],
                "type": record["type"],
                "observed_at": episode_instant(manifest, int(record["episode"])),
                "episode": int(record["episode"]),
            }
        )
    return out


# --- the run -------------------------------------------------------------------------
def run_simulation(
    manifest: Mapping[str, Any], seed: int, *, episodes: int | None = None
) -> SimulationRun:
    """Run the whole network for ``episode_budget`` simulated days on one seed.

    Pure: no clock, no socket, no database, no LLM. The only I/O is the seeded catalog the
    generator builds from ``fixtures/catalog/<seed_category>.json``.
    """
    from exchange.accept import accept
    from exchange.auction import (
        AuctionStateMachine,
        InMemoryAuctionStore,
        InMemoryLedgerSink,
        LedgerRecorder,
    )
    from exchange.auction.fanout import ArrivalClock
    from exchange.checkout.sellers import StaticRegisteredDomains
    from exchange.eligibility import BLACKLISTED, ELIGIBLE, StaticSellerEligibility
    from exchange.orchestration import solicit_bids
    from trust.events import InMemoryEventStore, append
    from trust.scoring import BLACKLIST_THRESHOLD, Blacklist, score
    from trust.snapshot import build_snapshot

    from fixtures.generator import generate

    scripted_behaviours(manifest)  # refuse a manifest that cannot supply a script, up front
    seed_category = _manifest_str(manifest, "seed_category")
    budget = _manifest_int(manifest, "episode_budget")
    if budget < FIRST_OBSERVED_EPISODE:
        raise SimulationError(f"manifest.episode_budget must be at least {FIRST_OBSERVED_EPISODE}")
    last = budget if episodes is None else int(episodes)
    if not 0 <= last <= budget:
        raise SimulationError(
            f"episodes={last} is outside the approved episode budget 0..{budget}; the bound "
            "on a simulation run is the human's, not the caller's"
        )

    # The adversary's whole campaign is drawn ONCE, from the one function that owns the
    # approved replay schedule, and then handed out an episode at a time. Re-deriving the
    # per-episode script inside the loop would be a second implementation of that schedule
    # living where nothing compares it to the first.
    campaign: dict[int, list[dict[str, Any]]] = {}
    for record in run_dishonest_campaign(manifest, seed, episodes=last):
        campaign.setdefault(int(record["episode"]), []).append(record)

    payload = generate(seed_category, seed)
    roster = build_roster(payload, manifest)
    dishonest_id = str((manifest.get("dishonest_store") or {}).get("store_id") or "")
    silent = frozenset(
        str(row["store_id"]) for row in roster if _SLOW_ROLE_MARKER in row["role"].lower()
    )
    domains = StaticRegisteredDomains(
        {str(row["store_id"]): str(row["store_domain"]) for row in roster}
    )

    ledger_sink = InMemoryLedgerSink()
    auctions = AuctionStateMachine(InMemoryAuctionStore(), LedgerRecorder(ledger_sink))
    event_store = InMemoryEventStore()
    blacklist = _seeded_blacklist(manifest, Blacklist)

    canonical: list[dict[str, Any]] = []
    raw_events: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    results: list[Episode] = []
    caught_at: int | None = None

    for episode in range(FIRST_OBSERVED_EPISODE, last + 1):
        deadline = _episode_epoch(manifest, episode) + BID_WINDOW_SECONDS
        as_of = episode_instant(manifest, episode)

        # 1. Eligibility is read from the trust the run has accumulated so far — which is
        #    the whole point of S2: a store the engine has caught stops being asked.
        statuses = {}
        for row in roster:
            store_id = str(row["store_id"])
            prior = score([o for o in observations if o["store_id"] == store_id], as_of=as_of)[
                "score"
            ]
            statuses[store_id] = BLACKLISTED if prior < BLACKLIST_THRESHOLD else ELIGIBLE
        eligibility = StaticSellerEligibility(statuses)

        # 2. The auction itself, through the real state machine.
        auction_id = f"sim-auction-{episode:02d}"
        auctions.create(
            auction_id,
            intent_id=str((manifest.get("fixture_intent") or {}).get("intent_id") or "intent-sim"),
            cluster_id=seed_category,
            roster=[dict(row) for row in roster],
            deadline=deadline,
        )
        auctions.open(auction_id, now=deadline - BID_WINDOW_SECONDS)

        solicitor = _ScriptedSolicitor(
            auction_id=auction_id,
            pitched_prices=_pitched_prices(manifest, roster, seed, episode),
            silent=silent,
            expires_at=deadline + EPISODE_SECONDS,
        )
        solicitation = solicit_bids(
            roster=roster,
            solicitor=solicitor,
            eligibility=eligibility,
            now=deadline,
            window=BID_WINDOW_SECONDS,
            # A frozen arrival clock: every answer lands at the instant the window opened,
            # so two runs stamp identically. Lateness is modelled by a store that does not
            # answer at all, which is what R10's timeout actually observes.
            clock=ArrivalClock(
                deadline, window=BID_WINDOW_SECONDS, monotonic=lambda: 0.0, started_at=0.0
            ),
        )
        auctions.close(auction_id, now=deadline)

        # 3. Pick a winner. Cheapest real bid, ties broken by store id — deterministic, and
        #    deliberately NAIVE: a shortlist that already knew about trust would hide the
        #    thing this simulation exists to demonstrate, which is trust catching the store
        #    that keeps winning on a price it does not honour.
        live = [entry for entry in solicitation.entries if not entry.fallback]
        live.sort(key=lambda entry: (entry.unit_price, entry.store_id))
        winner = live[0] if live else None

        accepted = False
        denial_reason: str | None = None
        episode_events: list[dict[str, Any]] = []
        if winner is not None:
            record = {
                "auction_id": auction_id,
                "intent_id": auction_id,
                "cluster_id": seed_category,
                "bids": [
                    {
                        "bid_id": f"{auction_id}-{entry.store_id}",
                        "store_id": entry.store_id,
                        "store_domain": domains.domain_for(entry.store_id),
                        "offer": dict(entry.offer),
                    }
                    for entry in live
                ],
                "accepted_bid_ref": None,
                "now": deadline,
            }
            outcome = accept(
                record,
                f"{auction_id}-{winner.store_id}",
                None,
                "redirect",
                registered_domains=domains,
            )
            accepted = bool(outcome.accepted)
            denial_reason = outcome.denial_reason
            episode_events = [dict(event) for event in outcome.events]
            if accepted:
                auctions.accept(auction_id, f"{auction_id}-{winner.store_id}", now=deadline)

        # 4. The tamper-evident record. Every event the exchange emitted goes in.
        for sequence, event in enumerate(episode_events):
            append(event_store, event)
            raw_events.append(event)
            canonical.append(normalise_event(event, episode=episode, sequence=sequence))

        # 5. What the honest winner's completed purchase is worth. Only a store that both
        #    won and was accepted transacts, so a store that never wins accrues no history
        #    at all — which is how the roster's "new store, no history" entry stays the
        #    low-data case the manifest says it is there to exercise.
        if accepted and winner is not None and winner.store_id != dishonest_id:
            observations.extend(honest_observations(manifest, winner.store_id, episode))

        # 6. The adversary's approved script for this episode, into the real trust engine.
        observations.extend(trust_observations(campaign.get(episode, ()), manifest))

        dishonest_score = score(
            [o for o in observations if o["store_id"] == dishonest_id], as_of=as_of
        )["score"]
        blacklisted_now = dishonest_score < BLACKLIST_THRESHOLD
        if blacklisted_now and caught_at is None:
            caught_at = episode

        results.append(
            Episode(
                episode=episode,
                auction_id=auction_id,
                solicited=tuple(solicitation.solicited),
                denied=tuple((denial.store_id, denial.status) for denial in solicitation.denied),
                bidders=tuple(entry.store_id for entry in live),
                fallbacks=tuple(
                    (entry.store_id, str(entry.fallback_reason))
                    for entry in solicitation.entries
                    if entry.fallback
                ),
                winner=winner.store_id if winner is not None else None,
                accepted=accepted,
                denial_reason=denial_reason,
                ledger_kinds=tuple(str(event.get("kind")) for event in episode_events),
                score=round(dishonest_score, 6),
                blacklisted=blacklisted_now,
            )
        )

    verdict = event_store.verify()
    final_as_of = episode_instant(manifest, max(last, FIRST_OBSERVED_EPISODE))
    snapshot = build_snapshot(
        [
            {
                "store_id": str(row["store_id"]),
                "business_identity": str(row["business_identity"]),
                "observations": [o for o in observations if o["store_id"] == str(row["store_id"])],
            }
            for row in roster
        ],
        blacklist=blacklist,
        as_of=final_as_of,
    )

    return SimulationRun(
        seed=int(seed),
        seed_category=seed_category,
        episodes=tuple(results),
        events=tuple(canonical),
        observations=tuple(observations),
        codes=tuple(minted_codes(raw_events)),
        head_hash=str(verdict.get("head_hash") or ""),
        chain_ok=bool(verdict.get("ok")),
        caught_at=caught_at,
        ledger_contract_problems=tuple(ledger_contract_problems(raw_events)),
        snapshot=snapshot,
    )


def _seeded_blacklist(manifest: Mapping[str, Any], blacklist_cls: Any) -> Any:
    """The platform blacklist as the approved roster describes it.

    The manifest's ``store-secondchance`` carries an **expired** entry, and it is seeded
    here rather than skipped: an expired blacklisting that still blocks is a store the
    platform never lets back in, and the only way to find that out is to have one.
    """
    blacklist = blacklist_cls()
    for store in manifest.get("stores") or []:
        if not isinstance(store, Mapping):
            continue
        role = str(store.get("role") or "").lower()
        if "expired" not in role:
            continue
        blacklist.add(
            business_identity=str(store.get("business_identity") or store.get("store_id")),
            reason_code="offer_integrity",
            status="expired",
            expires_at="2026-08-01T00:00:00Z",
            note=str(store.get("role") or ""),
        )
    return blacklist
