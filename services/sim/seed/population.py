"""The population that comes back — R14's loop, driven end to end through the served routes.

Everything the trust engine grades today is the store's own catalogue, the store's own prose, or
the transaction record the store's own checkout produced. R14 buyer feedback is the one signal
that is not the store talking about itself, the routes that collect it are built and served — and
**nothing has ever called them**, because a real prompt needs a shopper who comes back days after
delivery. This module is that shopper, several thousand times, on a seed.

What a run does, in order, per simulated day
--------------------------------------------
=====================================  ==============================================
step                                   what is driven
=====================================  ==============================================
who may be solicited at all            ``GET /snapshot`` on the trust service (R12)
who is asked, and by when              ``exchange.orchestration.solicit_bids`` (R10)
what the shopper is shown              ``exchange.ranking.rank`` — four slots (R2/R11)
accepting an offer                     ``exchange.accept.accept`` (D22/C10 host check)
what the merchant actually did         this module, from the approved delivery profile
what the platform MEASURED             ``trust.reconcile.reconcile`` (R4)
is there a prompt for this order       ``POST /buyer/feedback/prompt`` (R14)
what the shopper answers               :func:`seed.shoppers.answer_for`
recording the answer                   ``POST /buyer/feedback`` -> ``POST /events`` (R14)
what it did to the store               ``GET /snapshot`` again (R12)
=====================================  ==============================================

**The feedback goes over HTTP, always.** Never ``submit_feedback`` and never a ledger append: an
answer that does not travel through ``POST /buyer/feedback`` proves nothing about R14 and skips
the routed-buyer gate that route enforces, which is the one gate R14 *is*.

The answers are consequential, not decorative
----------------------------------------------
Each shopper's answer is derived from the reconciled verdict for *their own order* — the
platform's own measurement of what the store charged and when it shipped — and never from which
store it was. :mod:`seed.shoppers` holds that mapping and the argument for it. The consequence is
the sentence this whole feature exists to make true: a store that pitches well and delivers badly
accumulates ``feedback_match`` evidence against it, and ends up ranked below one that does both.

What this run deliberately does NOT send to trust
--------------------------------------------------
The reconciled verdicts themselves. ``trust.reconcile.observation_events`` would turn each one
into ``offer_integrity`` observations on ``price_honored`` / ``discount_honored`` /
``shipped_on_time`` and post them, and the postures would separate — but then the separation
would be attributable to the reconciliation seam, which already existed, and the demonstration
would say nothing about R14. So the reconciler is used **only** as the ground truth a shopper's
answer is derived from, and the only thing this population writes to the ledger is buyer
feedback. Every point of posture separation a run reports is therefore feedback's.

Labelled as simulated, everywhere
----------------------------------
Every ``order_ref`` this module mints starts with :data:`seed.shoppers.SIMULATED_ORDER_PREFIX`,
is asserted to before the submission is sent, and is copied verbatim onto the append-only ledger
event — so a posture built partly from synthetic feedback stays inspectable as such after this
process is gone. The transcript and the printed report say ``SIMULATED`` at the top. There is no
mode in which this writes an unlabelled answer.

That label is what makes this *seed* data rather than *fake* data, and it is only worth
writing if something can read it back: :mod:`seed.provenance` computes any store's posture with
the manufactured observations and without them, off the sealed chain, at an explicit instant.
Read that module before changing anything about the marker.

Run once, stored, replayed
---------------------------
This module simulates. It does not decide when to. ``python -m seed run`` drives it once and
hands what it produced to :mod:`seed.store`, which writes the canonical transcript, the ledger
chain the routes really wrote, and a provenance record with digests — the same
collect-once-replay-forever shape ``fixtures/real-catalogs/collection.json`` already uses.
``python -m seed replay`` then reproduces every posture from those bytes offline, with nothing
re-simulated and no service running.

Where it may point
-------------------
Loopback, unless the operator says otherwise on the command line. See :mod:`seed.targets`; the
guard runs on the resolved URLs at the top of :func:`run_population`, not only in the CLI, so
an in-process caller gets it too.

Determinism (S4 / C9)
----------------------
:func:`transcript` is the canonical form two runs of one seed compare byte for byte, and
:func:`transcript_digest` hashes it. Four families of value are excluded by name, each because
including them would assert a defect rather than a property:

* the minted discount code, checkout token and permalink — drawn from :mod:`secrets` (D22); a
  reproducible discount code is a *guessable* discount code. Their real values are checked for
  shape and uniqueness instead (:func:`minted_values`).
* the feedback ``event_id`` (a UUID minted by the buyer service) and its ``ts`` (a wall clock).
* the served posture's ``decayed_at``, which is the serve instant.
* the low-order digits of a decayed quantity, at a grid that is actually above the noise. This
  used to read "the measured drift on a full run is ~1e-11, five orders of magnitude below the
  rounding", and that number was wrong by five orders of magnitude in the other direction. With
  the approved 30-day half-life a run lasting 0.3 seconds already puts a deficit of 8.8e-7 on a
  ``beta`` of 11, which is LARGER than six-decimal rounding's 5e-7 half-grid — and measured, six
  full runs of one seed produced three distinct digests, every one differing by a single unit in
  the sixth place of one ``alpha`` or ``beta`` while every score matched. The repair is two
  grids rather than one: a Beta mean is invariant under decay, so ``score`` and ``confidence``
  keep :data:`POSTURE_PRECISION`, and the parameters, which are not, move to
  :data:`DIMENSION_PRECISION`. What is compared is the rounded posture *and* the ordering, which
  is what any claim about ranking actually rests on.
"""

from __future__ import annotations

__all__ = [
    "BID_WINDOW_SECONDS",
    "DEFAULT_MAX_DISCOUNT_PCT",
    "DEFAULT_SHOPPERS_PER_EPISODE",
    "DELIVERY_UNGRADEABLE_NOTICE",
    "DIMENSION_PRECISION",
    "POSTURE_PRECISION",
    "EpisodePostures",
    "PopulationError",
    "PopulationRun",
    "Purchase",
    "control_pair",
    "minted_values",
    "posture_view",
    "ranking_probe",
    "run_population",
    "transcript",
    "transcript_digest",
]

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sim.runner import BID_WINDOW_SECONDS, EPISODE_SECONDS, build_roster, episode_instant

from .shoppers import (
    SIMULATED_BUYER_PREFIX,
    SIMULATED_ORDER_PREFIX,
    ShopperPolicy,
    answer_for,
    delivered_outcome,
    delivery_profile,
    is_simulated_reference,
    promised_dispatch_days,
    shopper_rng,
)
from .targets import describe_target, require_local_target

#: How many shoppers each simulated day brings. Each one states an intent, gets their own
#: auction, and accepts one of the shortlist's differentiated slots — so a day is several
#: purchases spread across the shortlist rather than one purchase for whoever was cheapest.
DEFAULT_SHOPPERS_PER_EPISODE = 4

#: Decimal places a served ``score`` and ``confidence`` are rounded to in the canonical
#: transcript. See the module docstring: a served score carries the serve instant in its
#: low-order digits.
#:
#: Six is safe for these two and **only** for these two, and the reason is arithmetic rather
#: than taste: decay multiplies a dimension's ``alpha`` and its ``beta`` by the same factor,
#: and a Beta mean ``alpha / (alpha + beta)`` is invariant under that. Measured over 15 pairs
#: drawn from six full runs, ``score`` and ``confidence`` never differed by so much as one
#: unit in the sixth place. The dimension parameters themselves are a different quantity —
#: see :data:`DIMENSION_PRECISION`.
POSTURE_PRECISION = 6

#: Decimal places a dimension's ``alpha`` / ``beta`` are rounded to. **Not the same number as
#: above, and this is a defect being closed rather than a preference.**
#:
#: These are decayed absolute quantities, so unlike the score they carry the run's wall-clock
#: duration directly. With the approved 30-day half-life, a run lasting 0.3 seconds already
#: puts a deficit of 8.8e-7 on a ``beta`` of 11 — LARGER than the 5e-7 half-grid of six-decimal
#: rounding. So a transcript that rounded these at six places was claiming a byte-identity it
#: could not have at any realistic run length: measured, six full runs of one seed produced
#: THREE distinct transcript digests, differing by exactly one unit in the sixth place of a
#: single ``alpha`` or ``beta``, with every score identical. The suite's own determinism test
#: missed it because it runs two episodes, where the parameters are small enough that the
#: deficit rarely reaches a rounding boundary.
#:
#: Three places puts the grid two orders of magnitude above that noise — a half-grid of 5e-4
#: against a 4.4e-6 deficit for a 1.5-second run and 4.4e-5 for a fifteen-second one — and it
#: is the same precision :func:`_dim` already uses for the printed report, for the same stated
#: reason. Nothing observable is lost: these parameters are the prior plus sums of published
#: weights, so they land on multiples of 0.25, which are as far from a three-decimal rounding
#: boundary as a value can be.
DIMENSION_PRECISION = 3

#: The discount depth each simulated store's economic envelope authorises (R6). It has to be
#: SOMETHING: the exchange's price wall refuses a bid that declares a discount its roster row
#: authorises no depth for (measured — ``bid_price_unreconcilable`` on every bid), and the
#: approved manifest describes no envelopes. So this is the simulator stating an envelope for
#: its own simulated merchants, it is the same for every store so it cannot advantage one, and
#: it is reported in the run's header rather than left implicit.
DEFAULT_MAX_DISCOUNT_PCT = 30.0

#: Printed when a whole run produced no gradeable dispatch promise, which on this tree is every
#: run. It is a finding about ``apps/exchange``, not about this package, and it is printed rather
#: than swallowed because a population that quietly never reports lateness looks exactly like a
#: market where nothing ever ships late.
DELIVERY_UNGRADEABLE_NOTICE = (
    "REGRESSION: not one order in this run had a gradeable dispatch promise, so\n"
    "`shipped_on_time` was False for every order regardless of when it actually shipped, and\n"
    "no shopper could answer 'as_described_but_late'. A market where nothing is ever late\n"
    "reads exactly like a market nobody measured, which is why this prints rather than\n"
    "passing quietly.\n"
    "  This WAS true of every run: `apps/exchange/src/checkout/provider.py` projected the\n"
    "  accepted offer down to {product_ref, unit_price, total_price, discount} and dropped\n"
    "  `Offer.delivery_estimate_days`, while `trust.reconcile.reconciled_event` reads the\n"
    "  promise off precisely that event (`_promised(accepted)`). It is closed: the accepted\n"
    "  event carries the promise now, and both accept paths grade alike.\n"
    "  So seeing this again means the promise has stopped reaching the record that grades it.\n"
    "  Check what the `accepted` event carries before looking anywhere else — the comparison\n"
    "  itself (`RECONCILED_DIMENSIONS` maps delivery -> shipped_on_time) has always worked,\n"
    "  and the bids this population sends have always carried the field."
)

#: The discount depth a store actually pitches, inside that envelope. Below the cap on purpose:
#: SPEC's core tenet is that a market whose award rule is dominated by discount trains every
#: store to its maximum and then differentiates on nothing, and a population that pitched the
#: cap every time would be a small demonstration of exactly that.
PITCHED_DISCOUNT_PCT = 12.0


class PopulationError(RuntimeError):
    """The population cannot be run against this manifest, or against these services."""


# =====================================================================================
# What one run produced
# =====================================================================================
@dataclass(frozen=True)
class Purchase:
    """One shopper, one order, and what became of it.

    The reconciled verdict is carried alongside the answer on purpose: a reader has to be able
    to check that the answer tracks the outcome without re-running anything, and a transcript
    that recorded only "they said it did not match" would be a transcript in which the
    population's one load-bearing property is unverifiable.
    """

    episode: int
    shopper: int
    auction_id: str
    store_id: str
    slot: str
    order_ref: str
    accepted: bool
    denial_reason: str | None
    promised_total: float
    promised_delivery_days: float
    promised_discount_pct: float | None
    charged_total: float
    dispatch_days: float
    breached: bool
    price_honored: bool
    price_comparable: bool
    discount_honored: bool
    discount_comparable: bool
    shipped_on_time: bool
    delivery_comparable: bool
    outcome_choice: str
    responded: bool
    choice: str | None
    noisy: bool
    prompt_status: int
    prompt_offered: bool
    submit_status: int | None
    recorded_matched_pitch: bool | None
    recorded_reason: str | None

    def to_json(self) -> dict[str, Any]:
        """The canonical, comparable form. No token, no code, no event id, no clock."""
        return {
            "simulated": True,
            "episode": self.episode,
            "shopper": self.shopper,
            "auction_id": self.auction_id,
            "store_id": self.store_id,
            "slot": self.slot,
            "order_ref": self.order_ref,
            "accepted": self.accepted,
            "denial_reason": self.denial_reason,
            "promised_total": self.promised_total,
            "promised_delivery_days": self.promised_delivery_days,
            "promised_discount_pct": self.promised_discount_pct,
            "charged_total": self.charged_total,
            "dispatch_days": self.dispatch_days,
            "breached": self.breached,
            "price_honored": self.price_honored,
            "price_comparable": self.price_comparable,
            "discount_honored": self.discount_honored,
            "discount_comparable": self.discount_comparable,
            "shipped_on_time": self.shipped_on_time,
            "delivery_comparable": self.delivery_comparable,
            "outcome_choice": self.outcome_choice,
            "responded": self.responded,
            "choice": self.choice,
            "noisy": self.noisy,
            "prompt_status": self.prompt_status,
            "prompt_offered": self.prompt_offered,
            "submit_status": self.submit_status,
            "recorded_matched_pitch": self.recorded_matched_pitch,
            "recorded_reason": self.recorded_reason,
        }


@dataclass(frozen=True)
class EpisodePostures:
    """What ``GET /snapshot`` said about every store at the end of one simulated day."""

    episode: int
    stores: dict[str, dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {"episode": self.episode, "stores": self.stores}


@dataclass(frozen=True)
class PopulationRun:
    """Everything one seeded population produced, as plain data."""

    seed: int
    policy: ShopperPolicy
    shoppers_per_episode: int
    buyer_url: str
    trust_url: str
    purchases: tuple[Purchase, ...]
    postures: tuple[EpisodePostures, ...]
    opening_snapshot: dict[str, Any]
    closing_snapshot: dict[str, Any]
    profiles: dict[str, dict[str, Any]]
    minted: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    ledger_events: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def answered(self) -> tuple[Purchase, ...]:
        return tuple(p for p in self.purchases if p.responded)

    def to_json(self) -> dict[str, Any]:
        """The full record, including the values two runs may legitimately disagree on."""
        return {
            "simulated": True,
            "seed": self.seed,
            "policy": self.policy.to_json(),
            "shoppers_per_episode": self.shoppers_per_episode,
            "profiles": self.profiles,
            "purchases": [purchase.to_json() for purchase in self.purchases],
            "postures": [posture.to_json() for posture in self.postures],
            "opening_snapshot": posture_view(self.opening_snapshot),
            "closing_snapshot": posture_view(self.closing_snapshot),
        }


def posture_view(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """One served ``GET /snapshot`` body, reduced to what two runs of a seed must agree on.

    ``decayed_at`` is dropped (it is the serve instant); ``score`` and ``confidence`` are
    rounded to :data:`POSTURE_PRECISION` and the dimension parameters to the coarser
    :data:`DIMENSION_PRECISION`. **Two different grids, deliberately** — a Beta mean is
    invariant under decay and its parameters are not, so rounding both at six places was
    claiming a byte-identity the parameters cannot have. Both constants carry the measurement.

    ``feedback_match`` is kept dimension by dimension because it is the dimension this
    population writes to, and a reader has to be able to see that the other five did not move.
    """
    view: dict[str, dict[str, Any]] = {}
    for store_id, entry in sorted(snapshot.items()):
        if not isinstance(entry, Mapping):
            continue
        raw_dims = entry.get("dims")
        dims: Mapping[str, Any] = raw_dims if isinstance(raw_dims, Mapping) else {}
        view[str(store_id)] = {
            "score": round(float(entry.get("score") or 0.0), POSTURE_PRECISION),
            "confidence": round(float(entry.get("confidence") or 0.0), POSTURE_PRECISION),
            "low_data": bool(entry.get("low_data")),
            "blacklisted": bool(entry.get("blacklisted")),
            "dims": {
                str(name): {
                    "alpha": round(float(state.get("alpha") or 0.0), DIMENSION_PRECISION),
                    "beta": round(float(state.get("beta") or 0.0), DIMENSION_PRECISION),
                }
                for name, state in sorted(dims.items())
                if isinstance(state, Mapping)
            },
        }
    return view


def transcript(run: PopulationRun) -> dict[str, Any]:
    """The canonical run: the same seed must produce this byte for byte. See the docstring."""
    return run.to_json()


def transcript_digest(run: PopulationRun) -> str:
    """sha256 of the canonical transcript, which is what two runs are compared on."""
    canonical = json.dumps(transcript(run), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def minted_values(run: PopulationRun) -> dict[str, Any]:
    """The values the platform minted from :mod:`secrets` or a clock, and their properties.

    Reported rather than compared, exactly as :func:`sim.runner.minted_codes` is: a run whose
    discount codes were reproducible across seeds would be a run asserting a security defect.
    What IS asserted is that they exist, are well-formed, and never repeat.
    """
    return {
        "codes": list(run.minted),
        "unique_codes": len(set(run.minted)) == len(run.minted),
        "event_ids": list(run.event_ids),
        "unique_event_ids": len(set(run.event_ids)) == len(run.event_ids),
    }


# =====================================================================================
# The bidders
# =====================================================================================
class _PopulationSolicitor:
    """The outbound bid-request client for a population that makes DELIVERY commitments.

    A second solicitor beside :class:`sim.runner._ScriptedSolicitor`, and the difference is the
    whole point of this ticket rather than duplication for its own sake: the offers here carry a
    ``delivery_estimate_days`` and an envelope-provenanced ``discount``, which is what gives the
    reconciler a dispatch promise and a discount promise to grade. Without a promise on the
    record there is no such thing as breaking it, and ``shipped_on_time`` and
    ``discount_honored`` are ungradeable — which is exactly the state the existing run is in,
    and which its own test suite already records as a limit.

    The store the manifest marks "honest but slow" answers ``None``: it is eligible, it is
    asked, and it does not reply inside the window, so ``collect_bids`` represents it at list
    price with the reason ``no_response`` (R10) and it never becomes a purchase.
    """

    def __init__(
        self,
        *,
        auction_id: str,
        pitched_prices: Mapping[str, float],
        silent: frozenset[str],
        expires_at: float,
        promised_days: Mapping[str, float],
        observed_at: str,
    ) -> None:
        self._auction_id = auction_id
        self._prices = pitched_prices
        self._silent = silent
        self._expires_at = expires_at
        self._promised_days = promised_days
        self._observed_at = observed_at

    def solicit(self, store: Mapping[str, Any]) -> Mapping[str, Any] | None:
        store_id = str(store["store_id"])
        if store_id in self._silent:
            return None
        unit_price = self._prices.get(store_id)
        if unit_price is None:
            return None
        total = round(unit_price * (1.0 - PITCHED_DISCOUNT_PCT / 100.0), 2)
        return {
            "store_id": store_id,
            "bid": {
                "auction_id": self._auction_id,
                "store_id": store_id,
                "offer": {
                    "product_ref": str(store["product_ref"]),
                    "unit_price": unit_price,
                    "total_price": total,
                    "currency": "USD",
                    "checkout_url": (
                        f"https://{store['store_domain']}/cart/1:1?ref={self._auction_id}"
                    ),
                    "expires_at": self._expires_at,
                    "delivery_estimate_days": self._promised_days[store_id],
                    # R8/S5a: a hosted bid's discount must arrive through a provenance-tagged
                    # hook or the boundary refuses it (`bid_claim_unprovenanced`, measured).
                    # `envelope_rule` is the honest source — this depth comes from the store's
                    # approved economic envelope (R6) — and rank 1 is the published rank for it
                    # (`contracts.PROVENANCE_AUTHORITY_RANK`), not a number chosen here.
                    "discount": {
                        "type": "percentage",
                        "value": PITCHED_DISCOUNT_PCT,
                        "provenance": {
                            "source": "envelope_rule",
                            "ref": f"envelope://{store_id}/max_discount_pct",
                            "observed_at": self._observed_at,
                            "authority_rank": 1,
                        },
                    },
                },
                "claims": [],
            },
        }


def _pitched_prices(
    manifest: Mapping[str, Any], roster: Sequence[Mapping[str, Any]], seed: int, episode: int
) -> dict[str, float]:
    """What each store quotes this episode.

    The dishonest store quotes the price the **approved aggressive persona** pitches, so the
    headline number in this market is the one a human signed rather than one chosen here.
    Everyone else quotes their catalog price less a small seeded introductory discount — the
    same rule :func:`sim.runner._pitched_prices` uses, restated here because this population's
    stores are the same stores and a second pricing rule would make the two runs incomparable
    for no gain.
    """
    from sim.runner import _persona_unit_price

    dishonest_id = str((manifest.get("dishonest_store") or {}).get("store_id") or "")
    pitched = _persona_unit_price(manifest, "aggressive")
    out: dict[str, float] = {}
    for row in roster:
        store_id = str(row["store_id"])
        if store_id == dishonest_id and pitched is not None:
            out[store_id] = pitched
            continue
        rng = shopper_rng(seed, "quote", episode, store_id)
        out[store_id] = round(float(row["list_price"]) * (1.0 - rng.uniform(0.0, 0.08)), 2)
    return out


# =====================================================================================
# The two served doors this population drives
# =====================================================================================
class _Services:
    """The buyer and trust services, addressed over HTTP and nothing else.

    Deliberately thin. Every method below is one real request to one published route; there is
    no in-process shortcut and no ``if the service is unavailable, fall back to the library``
    branch, because such a branch is how a run comes back green having proved nothing.
    """

    def __init__(self, buyer_url: str, trust_url: str, client: Any) -> None:
        self._buyer = buyer_url.rstrip("/")
        self._trust = trust_url.rstrip("/")
        self._client = client

    def snapshot(self) -> dict[str, Any]:
        response = self._client.get(f"{self._trust}/snapshot")
        if response.status_code != 200:
            raise PopulationError(
                f"GET /snapshot answered {response.status_code}; the eligibility door the "
                f"exchange reads is unavailable, and a population that carried on would be "
                f"measuring nothing. Body: {response.text[:300]}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise PopulationError(f"GET /snapshot returned {type(body).__name__}, not a mapping")
        return body

    def prompt(self, order: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        response = self._client.post(f"{self._buyer}/buyer/feedback/prompt", json={"order": order})
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}

    def submit(self, order: Mapping[str, Any], answer: Mapping[str, Any]) -> tuple[int, Any]:
        if not is_simulated_reference(order.get("order_ref")):
            raise PopulationError(
                f"refusing to submit feedback for {order.get('order_ref')!r}: it does not carry "
                f"the {SIMULATED_ORDER_PREFIX!r} prefix. Synthetic sentiment reaches an "
                "append-only ledger with no delete; it is labelled or it is not sent."
            )
        response = self._client.post(
            f"{self._buyer}/buyer/feedback", json={"order": order, "response": answer}
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body


# =====================================================================================
# The run
# =====================================================================================
def run_population(
    manifest: Mapping[str, Any],
    seed: int,
    *,
    buyer_url: str,
    trust_url: str,
    client: Any,
    policy: ShopperPolicy | None = None,
    episodes: int | None = None,
    shoppers_per_episode: int = DEFAULT_SHOPPERS_PER_EPISODE,
    allow_remote: bool = False,
) -> PopulationRun:
    """Run the returning-shopper population against two real services.

    Args:
        manifest: the human-approved fixture manifest, digest chain already verified.
        seed: the run seed. Same seed, same population, byte-identical transcript.
        buyer_url: base URL of the buyer service (``POST /buyer/feedback``).
        trust_url: base URL of the trust service (``GET /snapshot``).
        client: an HTTP client with ``get``/``post``. Injected rather than constructed so a
            caller can bound its timeouts and so this function never mints a network client of
            its own.
        policy: the response rate and noise level. Defaults to :class:`ShopperPolicy`'s, whose
            numbers carry their justification beside them.
        episodes: shorten the run. Never longer than the manifest's approved ``episode_budget``.
        shoppers_per_episode: how many shoppers return each simulated day.
        allow_remote: the caller's explicit statement that a non-loopback service is intended.

    Raises:
        PopulationError: the manifest cannot describe this run, or a served door refused.
        UnsafeTarget: a URL points somewhere this tool must not manufacture trust signal.
    """
    from exchange.accept import accept
    from exchange.auction.fanout import ArrivalClock
    from exchange.checkout.sellers import StaticRegisteredDomains
    from exchange.eligibility import BLACKLISTED, ELIGIBLE, StaticSellerEligibility
    from exchange.orchestration import solicit_bids
    from exchange.ranking import rank
    from trust.reconcile import reconcile
    from trust.scoring import BLACKLIST_THRESHOLD

    from fixtures.generator import generate

    # Rule 4, and it runs HERE rather than only in the CLI: an in-process caller that hands this
    # function a production hostname gets the same refusal an operator would.
    buyer_url = require_local_target(buyer_url, allow_remote=allow_remote, what="buyer service")
    trust_url = require_local_target(trust_url, allow_remote=allow_remote, what="trust service")

    policy = policy or ShopperPolicy()
    if isinstance(shoppers_per_episode, bool) or not isinstance(shoppers_per_episode, int):
        raise PopulationError("shoppers_per_episode must be an integer")
    if shoppers_per_episode < 1:
        raise PopulationError(
            f"shoppers_per_episode must be at least 1, got {shoppers_per_episode}; a population "
            "with no shoppers answers no prompts and proves nothing"
        )

    budget = manifest.get("episode_budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise PopulationError("manifest.episode_budget must be a positive integer")
    last = budget if episodes is None else int(episodes)
    if not 1 <= last <= budget:
        raise PopulationError(
            f"episodes={last} is outside the approved episode budget 1..{budget}; the bound on "
            "a simulation run is the human's, not the caller's"
        )

    seed_category = str(manifest.get("seed_category") or "")
    if not seed_category:
        raise PopulationError("manifest.seed_category must be a non-empty string")

    payload = generate(seed_category, seed)
    roster = build_roster(payload, manifest)
    for row in roster:
        # The store's approved economic envelope (R6). Stated here, once, and identically for
        # every store — see DEFAULT_MAX_DISCOUNT_PCT for why it has to exist at all.
        row["max_discount_pct"] = DEFAULT_MAX_DISCOUNT_PCT

    stores_by_id = {str(row["store_id"]): row for row in roster}
    profiles = {store_id: delivery_profile(row).to_json() for store_id, row in stores_by_id.items()}
    promised_days = {
        store_id: promised_dispatch_days(manifest, store_id) for store_id in stores_by_id
    }
    silent = frozenset(
        store_id for store_id, row in stores_by_id.items() if "slow" in str(row["role"]).lower()
    )
    domains = StaticRegisteredDomains({s: str(r["store_domain"]) for s, r in stores_by_id.items()})
    services = _Services(buyer_url, trust_url, client)

    # The fixture intent's PREFERENCES, with its hard constraints deliberately dropped. R19 says
    # a hard constraint requires verified supporting facts, and the bids this population mints
    # carry no verified catalog attributes — measured, stating `roast_level=light` and
    # `net_weight=250 g` filtered every candidate and produced an EMPTY shortlist, every
    # episode. A population where nobody is ever shown anything answers no prompts. That is a
    # limit of these synthetic bids, not of the ranker, and it is stated rather than closed by
    # relaxing R19.
    intent = dict(manifest.get("fixture_intent") or {})
    intent["constraints"] = []
    intent["hard_constraints"] = []

    purchases: list[Purchase] = []
    postures: list[EpisodePostures] = []
    minted: list[str] = []
    event_ids: list[str] = []
    ledger_events: list[dict[str, Any]] = []

    opening = services.snapshot()

    for episode in range(1, last + 1):
        as_of = episode_instant(manifest, episode)
        deadline = _episode_epoch(manifest, episode)
        served = services.snapshot()
        # Eligibility read from the SERVED door rather than from a local score. This is the loop
        # closing: a store the shoppers have sunk stops being asked, and it stops being asked
        # because `GET /snapshot` said so.
        statuses = {}
        for store_id in stores_by_id:
            entry = served.get(store_id)
            sunk = not isinstance(entry, Mapping) or bool(entry.get("blacklisted"))
            sunk = sunk or float(entry.get("score", 0.0)) < BLACKLIST_THRESHOLD  # type: ignore[union-attr]
            statuses[store_id] = BLACKLISTED if sunk else ELIGIBLE
        eligibility = StaticSellerEligibility(statuses)
        prices = _pitched_prices(manifest, roster, seed, episode)

        for shopper in range(shoppers_per_episode):
            auction_id = f"sim-fb-a{episode:02d}-{shopper:02d}"
            solicitation = solicit_bids(
                roster=roster,
                solicitor=_PopulationSolicitor(
                    auction_id=auction_id,
                    pitched_prices=prices,
                    silent=silent,
                    expires_at=deadline + EPISODE_SECONDS,
                    promised_days=promised_days,
                    observed_at=as_of,
                ),
                eligibility=eligibility,
                now=deadline,
                window=BID_WINDOW_SECONDS,
                # A frozen arrival clock, for the reason `sim.runner` gives: every answer lands
                # at the instant the window opened, so two runs stamp identically, and lateness
                # is modelled by a store that does not answer at all.
                clock=ArrivalClock(
                    deadline, window=BID_WINDOW_SECONDS, monotonic=lambda: 0.0, started_at=0.0
                ),
            )
            live = [entry for entry in solicitation.entries if not entry.fallback]
            if not live:
                continue

            candidates: list[dict[str, Any]] = [
                {
                    "bid_id": f"{auction_id}-{entry.store_id}",
                    "store_id": entry.store_id,
                    "store_domain": domains.domain_for(entry.store_id),
                    "offer": dict(entry.offer),
                    "claims": [],
                }
                for entry in live
            ]
            ranked = rank(candidates, intent, served, {"now": deadline, "auction_id": auction_id})
            slots = list(ranked["shortlist"]["slots"])
            if not slots:
                continue
            # R2: up to four DIFFERENTIATED slots, and the point of them is that the cheapest
            # offer does not take every shopper. Shoppers are spread across the slots the real
            # ranker produced, deterministically, so every shortlisted store transacts and can
            # therefore be graded — a store cannot escape feedback by being second.
            slot = slots[shopper % len(slots)]
            bid_ref = str(slot["bid_ref"])
            chosen = next(c for c in candidates if c["bid_id"] == bid_ref)
            store_id = str(chosen["store_id"])

            record = {
                "auction_id": auction_id,
                "intent_id": str(intent.get("intent_id") or auction_id),
                "cluster_id": seed_category,
                "bids": [
                    {
                        "bid_id": c["bid_id"],
                        "store_id": c["store_id"],
                        "store_domain": c["store_domain"],
                        "offer": c["offer"],
                    }
                    for c in candidates
                ],
                "accepted_bid_ref": None,
                "now": deadline,
            }
            outcome = accept(record, bid_ref, None, "redirect", registered_domains=domains)
            order_ref = f"{SIMULATED_ORDER_PREFIX}{episode:02d}-{shopper:02d}-{store_id}"
            accept_events = [dict(event) for event in outcome.events]
            ledger_events.extend(accept_events)
            minted.extend(_codes_of(accept_events))

            if not outcome.accepted:
                purchases.append(
                    _denied_purchase(
                        episode,
                        shopper,
                        auction_id,
                        store_id,
                        str(slot["slot"]),
                        order_ref,
                        outcome.denial_reason,
                    )
                )
                continue

            chosen_offer = chosen["offer"]
            if not isinstance(chosen_offer, Mapping):
                raise PopulationError(
                    f"the accepted bid {bid_ref!r} carries no offer, so there is no promise to "
                    "grade the delivery against"
                )
            promise = _promise_of(chosen_offer)
            profile = delivery_profile(stores_by_id[store_id])
            delivered = delivered_outcome(profile, promise, seed, order_ref)
            merchant_events = _merchant_events(
                manifest=manifest,
                episode=episode,
                store_id=store_id,
                order_ref=order_ref,
                accept_events=accept_events,
                delivered=delivered,
            )
            reconciled = reconcile([*accept_events, *merchant_events])
            if not reconciled:
                raise PopulationError(
                    f"the platform could not reconcile order {order_ref!r} from its own event "
                    "stream, so there is no measured outcome for a shopper to report on"
                )
            verdict = dict(reconciled[0]["payload"])

            order_body = {
                "order_ref": order_ref,
                "store_id": store_id,
                "auction_id": auction_id,
                "routed": True,
                "status": "delivered",
                "buyer_pseudonym": (
                    f"{SIMULATED_BUYER_PREFIX}"
                    f"{shopper_rng(seed, 'pseudonym', order_ref).getrandbits(24):06x}"
                ),
            }
            prompt_status, prompt_body = services.prompt(order_body)
            if prompt_status != 200 or not prompt_body.get("offered"):
                raise PopulationError(
                    f"the network routed order {order_ref!r} and POST /buyer/feedback/prompt "
                    f"answered {prompt_status} {prompt_body!r}. R14's gate is 'only "
                    "network-routed buyers'; a routed order with no prompt is the gate refusing "
                    "the case it exists to admit."
                )
            prompt = prompt_body["prompt"]
            answer = answer_for(prompt, verdict, policy, seed, order_ref)

            submit_status: int | None = None
            recorded_matched: bool | None = None
            recorded_reason: str | None = None
            if answer.responded and answer.choice is not None:
                submit_status, submitted = services.submit(
                    order_body,
                    {"question_id": str(prompt["question_id"]), "choice": answer.choice},
                )
                if submit_status != 201 or not isinstance(submitted, Mapping):
                    raise PopulationError(
                        f"POST /buyer/feedback answered {submit_status} for {order_ref!r}: "
                        f"{submitted!r}. The shopper's answer did not become a ledger event."
                    )
                recorded_matched = bool(submitted.get("matched_pitch"))
                recorded_reason = str(submitted.get("reason") or "")
                event_ids.append(str(submitted.get("event_id") or ""))

            purchases.append(
                Purchase(
                    episode=episode,
                    shopper=shopper,
                    auction_id=auction_id,
                    store_id=store_id,
                    slot=str(slot["slot"]),
                    order_ref=order_ref,
                    accepted=True,
                    denial_reason=None,
                    promised_total=float(promise["total_price"]),
                    promised_delivery_days=float(promise["delivery_estimate_days"]),
                    promised_discount_pct=promise["discount_percentage"],
                    charged_total=float(delivered["charged_total"]),
                    dispatch_days=float(delivered["dispatch_days"]),
                    breached=bool(delivered["breached"]),
                    price_honored=bool(verdict.get("price_honored")),
                    price_comparable=bool(verdict.get("price_comparable")),
                    discount_honored=bool(verdict.get("discount_honored")),
                    discount_comparable=bool(verdict.get("discount_comparable")),
                    shipped_on_time=bool(verdict.get("shipped_on_time")),
                    delivery_comparable=bool(verdict.get("delivery_comparable")),
                    outcome_choice=answer.outcome_choice,
                    responded=answer.responded,
                    choice=answer.choice,
                    noisy=answer.noisy,
                    prompt_status=prompt_status,
                    prompt_offered=True,
                    submit_status=submit_status,
                    recorded_matched_pitch=recorded_matched,
                    recorded_reason=recorded_reason,
                )
            )

        postures.append(EpisodePostures(episode, posture_view(services.snapshot())))

    closing = services.snapshot()
    return PopulationRun(
        seed=int(seed),
        policy=policy,
        shoppers_per_episode=shoppers_per_episode,
        buyer_url=buyer_url,
        trust_url=trust_url,
        purchases=tuple(purchases),
        postures=tuple(postures),
        opening_snapshot=opening,
        closing_snapshot=closing,
        profiles=profiles,
        minted=tuple(minted),
        event_ids=tuple(event_ids),
        ledger_events=tuple(ledger_events),
    )


def _episode_epoch(manifest: Mapping[str, Any], episode: int) -> float:
    """The episode's instant as a float epoch — the exchange's clock frame. Never ``now()``."""
    from datetime import datetime

    approval = manifest.get("approval")
    stamp = approval.get("approved_at") if isinstance(approval, Mapping) else None
    if not isinstance(stamp, str) or not stamp.strip():
        raise PopulationError(
            "manifest.approval.approved_at must be the ISO-8601 instant the manifest was "
            "approved; the population counts its episode days from it rather than from a clock"
        )
    base = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    return (base + timedelta(days=max(0, episode - 1))).timestamp()


def _denied_purchase(
    episode: int,
    shopper: int,
    auction_id: str,
    store_id: str,
    slot: str,
    order_ref: str,
    denial_reason: str | None,
) -> Purchase:
    """A shopper whose acceptance was refused. There is no order, so there is no prompt.

    Recorded rather than dropped: an episode in which the exchange refused every acceptance
    looks, in a transcript that only kept purchases, exactly like an episode with no shoppers.
    """
    return Purchase(
        episode=episode,
        shopper=shopper,
        auction_id=auction_id,
        store_id=store_id,
        slot=slot,
        order_ref=order_ref,
        accepted=False,
        denial_reason=denial_reason,
        promised_total=0.0,
        promised_delivery_days=0.0,
        promised_discount_pct=None,
        charged_total=0.0,
        dispatch_days=0.0,
        breached=False,
        price_honored=False,
        price_comparable=False,
        discount_honored=False,
        discount_comparable=False,
        shipped_on_time=False,
        delivery_comparable=False,
        outcome_choice="",
        responded=False,
        choice=None,
        noisy=False,
        prompt_status=0,
        prompt_offered=False,
        submit_status=None,
        recorded_matched_pitch=None,
        recorded_reason=None,
    )


def _promise_of(offer: Mapping[str, Any]) -> dict[str, Any]:
    """The promise this offer made, read off the offer the market really accepted."""
    discount = offer.get("discount")
    percentage = (
        float(discount["value"])
        if isinstance(discount, Mapping)
        and str(discount.get("type", "percentage")).lower() == "percentage"
        and isinstance(discount.get("value"), (int, float))
        else None
    )
    return {
        "total_price": float(offer.get("total_price") or 0.0),
        "delivery_estimate_days": float(offer.get("delivery_estimate_days") or 0.0),
        "discount_percentage": percentage,
    }


def _codes_of(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every discount code a ``code_created`` event names. Read off that kind only.

    The same rule :func:`sim.runner.minted_codes` states: the code is repeated on the
    accompanying ``checkout_redirect``, so scanning every event counts each mint twice and turns
    "codes never repeat" into an assertion that can only fail.
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


def _merchant_events(
    *,
    manifest: Mapping[str, Any],
    episode: int,
    store_id: str,
    order_ref: str,
    accept_events: Sequence[Mapping[str, Any]],
    delivered: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """What the merchant's own surfaces reported about this order (R4).

    Two events, in the shapes the platform really receives them:

    * ``order_paid`` — the authoritative webhook. It carries the MERCHANT's checkout token,
      which is a different value from the exchange's, exactly as a real Shopify checkout does;
      the two halves meet on the single-use discount code, which is the join
      ``trust.reconcile`` documents at length and which is exercised here rather than short-
      circuited by echoing the exchange's token back.
    * ``order_fulfilled`` — when the parcel actually left, which the webhook cannot know.

    ``discount_applications`` uses the ``{"type": "percentage", "value": ...}`` shape this
    repository's own producers emit, which ``trust.reconcile._is_percentage_application`` reads.
    """
    from datetime import datetime

    token = ""
    code = ""
    for event in accept_events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        token = token or str(payload.get("checkout_token") or "")
        for key in ("code", "discount_code"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                code = code or value
    if not code:
        raise PopulationError(
            f"the checkout for {order_ref!r} minted no discount code, so the webhook and the "
            "offer have no key to meet on"
        )

    paid_at = episode_instant(manifest, episode)
    dispatched = datetime.fromisoformat(paid_at.replace("Z", "+00:00")) + timedelta(
        days=float(delivered["dispatch_days"])
    )
    applied = delivered["applied_discount_percentage"]
    # The merchant's own token: a different value from the exchange's, derived from it so the
    # two are never accidentally equal and never accidentally the same across two orders.
    merchant_token = hashlib.sha256(f"merchant:{token}:{order_ref}".encode()).hexdigest()[:32]

    return [
        {
            "event_id": f"paid:{order_ref}",
            "ts": paid_at,
            "kind": "order_paid",
            "store_id": store_id,
            "order_ref": order_ref,
            "payload": {
                "checkout_token": merchant_token,
                "order_ref": order_ref,
                "total_price": float(delivered["charged_total"]),
                "discount_codes": [{"code": code}],
                "discount_applications": (
                    [{"type": "percentage", "value": float(applied)}] if applied is not None else []
                ),
            },
        },
        {
            "event_id": f"fulfilled:{order_ref}",
            "ts": dispatched.isoformat().replace("+00:00", "Z"),
            "kind": "order_fulfilled",
            "store_id": store_id,
            "order_ref": order_ref,
            "payload": {
                "order_ref": order_ref,
                "fulfilled_at": dispatched.isoformat().replace("+00:00", "Z"),
            },
        },
    ]


# =====================================================================================
# The sentence the whole feature exists to make true
# =====================================================================================
def ranking_probe(
    snapshot: Mapping[str, Any], good_store: str, bad_store: str, *, now: float
) -> dict[str, Any]:
    """Rank two offers that differ in NOTHING except which store made them.

    Same product, same price, same delivery promise, same (absent) claims, same domain shape.
    The only input that differs is the trust posture ``GET /snapshot`` served, so whatever the
    ranker does here it does *because of the feedback*. Run over the opening snapshot and again
    over the closing one, this is the proof that a store which pitches well and delivers badly
    ends up ranked below one that does both.

    Returns ``{"order": [store_id, ...], "scores": {store_id: rank_score}}``.
    """
    from exchange.ranking import rank

    def candidate(store_id: str) -> dict[str, Any]:
        return {
            "bid_id": f"probe-{store_id}",
            "store_id": store_id,
            "store_domain": f"{store_id}.example.com",
            "offer": {
                "product_ref": "probe-product",
                "unit_price": 20.0,
                "total_price": 20.0,
                "currency": "USD",
                "checkout_url": f"https://{store_id}.example.com/cart/1:1",
                "expires_at": now + EPISODE_SECONDS,
                "delivery_estimate_days": 2.0,
            },
            "claims": [],
        }

    ranked = rank(
        [candidate(good_store), candidate(bad_store)],
        {"intent_id": "probe", "constraints": [], "hard_constraints": []},
        snapshot,
        {"now": now, "auction_id": "probe"},
    )
    rows = list(ranked["ranked"])
    return {
        "order": [str(row.get("store_id")) for row in rows],
        "scores": {
            str(row.get("store_id")): round(float(row.get("rank_score") or 0.0), 6) for row in rows
        },
    }


def control_pair(run: PopulationRun, manifest: Mapping[str, Any]) -> tuple[str, str]:
    """``(over_promising_store, honest_control_store)`` — both named by the approved manifest.

    NOT "the worst-scoring store and the best-scoring one". Picking the winner after the fact
    would make the claim unfalsifiable: with five stores and noise, *some* promise-keeping store
    almost always ends above the over-promising one, and reporting that pair would be reporting
    the maximum of a sample as if it were an effect. So the pair is fixed before the run: the
    manifest's ``dishonest_store`` — which is also the only store the roster marks
    ``honest: false`` and therefore the only one this population gives the over-promising
    delivery profile — and the store its ``personas.honest`` names as the control.

    A run in which THAT pair does not separate is a run that failed, and it will say so.
    """
    over = [
        store_id for store_id, p in sorted(run.profiles.items()) if p["name"] == "over_promises"
    ]
    if not over:
        raise PopulationError(
            "the approved roster marks no store dishonest, so this population has nobody who "
            "pitches well and delivers badly and there is no pair to separate"
        )
    personas = manifest.get("personas")
    control = ""
    if isinstance(personas, Mapping):
        honest = personas.get("honest")
        if isinstance(honest, Mapping):
            control = str(honest.get("store_id") or "")
    if not control:
        raise PopulationError(
            "manifest.personas.honest names no store_id, so there is no approved control to "
            "compare the over-promising store against"
        )
    transacted = {p.store_id for p in run.purchases if p.accepted}
    if control not in transacted:
        raise PopulationError(
            f"the approved honest control {control!r} never transacted in this run, so it "
            "accumulated no buyer feedback and cannot be compared against anything"
        )
    return over[0], control


def _separated(run: PopulationRun, manifest: Mapping[str, Any]) -> bool:
    """Did the postures separate in the direction the outcomes justify?

    The run's exit status, and deliberately not "did it complete". A population that answered
    every prompt and left two stores indistinguishable has demonstrated that R14's channel does
    nothing, which must not look like success.
    """
    try:
        bad, good = control_pair(run, manifest)
    except PopulationError:
        return False
    bad_score = float(run.closing_snapshot.get(bad, {}).get("score") or 0.0)
    good_score = float(run.closing_snapshot.get(good, {}).get("score") or 0.0)
    return good_score > bad_score


def _summarise(run: PopulationRun, manifest: Mapping[str, Any]) -> None:
    """The human report. Says SIMULATED first and says what did not happen as loudly as what did."""
    answered = run.answered
    noisy = [p for p in answered if p.noisy]
    print("=" * 78)
    print("SIMULATED BUYER FEEDBACK — every answer below was generated, not collected.")
    print("=" * 78)
    print(f"buyer   {describe_target(run.buyer_url)}")
    print(f"trust   {describe_target(run.trust_url)}")
    print(
        f"seed={run.seed} shoppers/day={run.shoppers_per_episode} "
        f"response_rate={run.policy.response_rate} noise_rate={run.policy.noise_rate}"
    )
    accepted = [p for p in run.purchases if p.accepted]
    print(
        f"purchases={len(accepted)} prompted={len(accepted)} answered={len(answered)} "
        f"({_pct(len(answered), len(accepted))} against a configured "
        f"{run.policy.response_rate:.0%}) noisy={len(noisy)} "
        f"({_pct(len(noisy), len(answered))} of answers did NOT track the outcome, against a "
        f"configured {run.policy.noise_rate:.0%})"
    )
    print()
    print("delivery profiles, read out of the approved manifest:")
    for store_id, profile in sorted(run.profiles.items()):
        print(f"  {store_id:<22} {profile['name']:<16} breach_rate={profile['breach_rate']}")
    print()
    print("what the shoppers said, by store and by measured outcome:")
    for store_id in sorted({p.store_id for p in accepted}):
        rows = [p for p in accepted if p.store_id == store_id]
        said = [p for p in rows if p.responded]
        breaches = [p for p in rows if p.breached]
        negative = [p for p in said if p.recorded_matched_pitch is False]
        print(
            f"  {store_id:<22} orders={len(rows):<3} breached={len(breaches):<3} "
            f"answered={len(said):<3} said-no-match={len(negative)}"
        )
    print()
    if accepted and not any(purchase.delivery_comparable for purchase in accepted):
        print(DELIVERY_UNGRADEABLE_NOTICE)
        print()
    print("the outcome that drove each answer — a sample, two orders per store, showing the")
    print("promise, what the merchant did, what the reconciler measured, and what was said:")
    for store_id in sorted({p.store_id for p in accepted}):
        for purchase in [p for p in accepted if p.store_id == store_id and p.responded][:2]:
            print(f"  {purchase.order_ref}")
            print(
                f"    promised {purchase.promised_total:.2f} in "
                f"{purchase.promised_delivery_days:.2f}d at "
                f"{purchase.promised_discount_pct or 0:.0f}% off"
                f"  ->  charged {purchase.charged_total:.2f} in "
                f"{purchase.dispatch_days:.2f}d"
            )
            print(
                f"    reconciled price_honored={purchase.price_honored} "
                f"discount_honored={purchase.discount_honored} "
                f"shipped_on_time={purchase.shipped_on_time}"
            )
            print(
                f"    outcome justifies {purchase.outcome_choice!r}; shopper said "
                f"{purchase.choice!r}"
                f"{'  <- NOISE, this answer does not track the outcome' if purchase.noisy else ''}"
            )
    print()
    print("posture on GET /snapshot, before and after (feedback_match is the ONLY dimension")
    print("this population writes to; the other five are shown so you can see they did not move):")
    for store_id in sorted(run.closing_snapshot):
        before = float(run.opening_snapshot.get(store_id, {}).get("score") or 0.0)
        after = float(run.closing_snapshot.get(store_id, {}).get("score") or 0.0)
        profile = run.profiles.get(store_id, {}).get("name", "-")
        fm_before = _dim(run.opening_snapshot, store_id, "feedback_match")
        fm_after = _dim(run.closing_snapshot, store_id, "feedback_match")
        others = sorted(
            name
            for name in _dim_names(run.closing_snapshot, store_id)
            if name != "feedback_match"
            and _dim(run.closing_snapshot, store_id, name)
            != _dim(run.opening_snapshot, store_id, name)
        )
        moved = ", ".join(others) if others else "none"
        print(f"  {store_id:<22} {before:.4f} -> {after:.4f}  ({after - before:+.4f})  {profile}")
        print(f"    feedback_match  Beta{fm_before} -> Beta{fm_after}    other dims moved: {moved}")
    print()
    try:
        bad, good = control_pair(run, manifest)
    except PopulationError as exc:
        print(f"NOT DEMONSTRATED: {exc}")
        return
    now = _episode_epoch(manifest, 1)
    before_probe = ranking_probe(run.opening_snapshot, good, bad, now=now)
    after_probe = ranking_probe(run.closing_snapshot, good, bad, now=now)
    print("two offers identical in everything except which store made them, ranked by the")
    print("real exchange ranker against the served snapshot (before: a tie broken by store id,")
    print("because nobody has any evidence yet — it is not a preference):")
    print(f"  before  {' > '.join(before_probe['order'])}   {before_probe['scores']}")
    print(f"  after   {' > '.join(after_probe['order'])}   {after_probe['scores']}")
    print()
    if _separated(run, manifest):
        print(
            f"CLOSED: the approved control {good} (kept its promises) ends above {bad} "
            f"(pitched well, delivered badly) on the door the exchange reads."
        )
    else:
        print(
            f"NOT CLOSED: {bad} was not scored below the approved control {good}. Buyer "
            "feedback moved nothing the exchange can see, which is the failure this run "
            "exists to detect."
        )


def _dim_names(snapshot: Mapping[str, Any], store_id: str) -> list[str]:
    entry = snapshot.get(store_id)
    dims = entry.get("dims") if isinstance(entry, Mapping) else None
    return sorted(dims) if isinstance(dims, Mapping) else []


def _dim(snapshot: Mapping[str, Any], store_id: str, name: str) -> tuple[float, float]:
    """One dimension's ``(alpha, beta)``, rounded so a serve-instant decay does not show."""
    entry = snapshot.get(store_id)
    dims = entry.get("dims") if isinstance(entry, Mapping) else None
    state = dims.get(name) if isinstance(dims, Mapping) else None
    if not isinstance(state, Mapping):
        return (0.0, 0.0)
    return (
        round(float(state.get("alpha") or 0.0), 3),
        round(float(state.get("beta") or 0.0), 3),
    )


def _pct(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{100.0 * part / whole:.0f}%"
