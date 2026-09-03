"""The merged observation framework: one Beta per dimension, one score, one confidence.

R12 / S2 / S3. Every trust signal this system has — a verification outcome on a pitched
claim, a reconciled transaction, a buyer's feedback cross-checked against a return — is the
same kind of thing here: a **trust observation**, ``{store_id, dim, type, observed_at}``,
landing on exactly one of the six dimensions with the weight the approved manifest publishes
for its type. There is no second scoring system for verification and no third for feedback;
that is what "merged" means, and it is why a store with no transactions at all still has a
score that moved.

The model
--------
Each dimension carries a Beta posterior that starts at the neutral, low-confidence prior
``Beta(2, 2)`` — deliberately interior, so a brand-new store is neither trusted nor
condemned, and so the *confidence* rather than the *score* is what says "we have not seen
much yet". An observation adds its published weight to ``alpha`` (positive) or ``beta``
(negative), scaled by exponential time decay against the recorded ``as_of`` instant.

======================  ========  =========================================================
observation type        weight    what it means
======================  ========  =========================================================
``verified``            1.0       a pitched claim checked out against the catalog
``fulfilled``           1.0       a promise the transaction record shows was kept
``unsupported``         0.5       no evidence either way — a SMALL published negative whose
                                  dominant effect is on coverage and confidence (D53/R19)
``ambiguous``           0.0       decides nothing: ZERO mean movement, coverage only
``contradicted``        2.0       the catalog or the transaction says otherwise
``mismatch_return``     1.5       the buyer said it matched and then returned it
``severe_policy``       3.0       an advertised policy refused outright
======================  ========  =========================================================

Coverage, and why undecided outcomes go there instead of into the mean
----------------------------------------------------------------------
``unsupported`` and ``ambiguous`` are not evidence *about the store*; they are evidence about
how much the verifier could actually see. Letting them move the mean like a contradiction
would score a seller for a catalog that was merely thin, and letting them move nothing at all
would let a seller buy a clean record by pitching only unfalsifiable claims. So every
dimension publishes ``coverage`` — the share of its observations that actually decided
something — and confidence is the product of how much evidence there is and how much of it
decided anything. ``ambiguous`` therefore leaves ``(alpha, beta)`` untouched to the last bit
and still costs the store confidence.

Determinism (S3)
----------------
``as_of`` is an explicit instant, never a clock: decay is evaluated against it, the served
``decayed_at`` records it, and observations are folded in input order. Two runs over the same
observations in the same order produce identical floats, which is what makes
``replay(events, as_of=...) == score(observations, as_of=...)`` a real assertion rather than
a comparison of two approximations. Nothing here reads the wall clock, and nothing here is
random.

Numerics: standard library only. ``scipy``/``numpy`` are deliberately absent from every
service image in this repo, and a Beta posterior needs neither.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from .dimensions import TRUST_DIMENSIONS, require_trust_dimension
from .manifest import manifest_int, manifest_mapping, manifest_number

__all__ = [
    "BLACKLIST_THRESHOLD",
    "CONFIDENCE_EVIDENCE_HALF_LIFE",
    "CONFIDENCE_FLOOR",
    "DECIDING_OBSERVATION_TYPES",
    "HALF_LIFE_DAYS",
    "NEW_STORE_PRIOR_N",
    "OBSERVATION_POLARITY",
    "OBSERVATION_WEIGHTS",
    "PRIOR_ALPHA",
    "PRIOR_BETA",
    "PUBLISHED_OBSERVATION_WEIGHTS",
    "SCORE_VERSION",
    "UnknownObservationType",
    "decay_factor",
    "prior_snapshot",
    "score",
]

#: Bumped whenever a change to this file could move a served number. It is carried on every
#: snapshot so a replayed score can be compared against the served one *and* against the
#: version of the arithmetic that produced it — comparing two scores from two versions is
#: comparing two different questions.
SCORE_VERSION = "trust-score-1.0.0"


class UnknownObservationType(LookupError):
    """An observation type with no published weight.

    Loud on purpose, and for the same reason as ``UnmappedClaimType``: a type nobody weighted
    would otherwise be silently dropped, and a dishonest behaviour that produced only that
    type would score as a clean record.
    """


#: The published observation weights, as they appear in the approved manifest at
#: ``manifest_version`` 1.0.0 / DESIGN "Reconciliation (approved)". Used only when the
#: manifest is unreachable — see :mod:`.manifest`.
PUBLISHED_OBSERVATION_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "verified": 1.0,
        "fulfilled": 1.0,
        "unsupported": 0.5,
        "ambiguous": 0.0,
        "contradicted": 2.0,
        "mismatch_return": 1.5,
        "severe_policy": 3.0,
    }
)

#: Which side of the Beta each observation type lands on. ``"neutral"`` moves neither, which
#: is stronger than "moves them by zero": it is why ``ambiguous`` leaves ``(alpha, beta)``
#: bit-identical to the prior rather than merely close to it.
OBSERVATION_POLARITY: Mapping[str, str] = MappingProxyType(
    {
        "verified": "positive",
        "fulfilled": "positive",
        "unsupported": "negative",
        "ambiguous": "neutral",
        "contradicted": "negative",
        "mismatch_return": "negative",
        "severe_policy": "negative",
    }
)

#: The observation types that *decide* something. ``unsupported`` and ``ambiguous`` are
#: absent by design: they are the two outcomes that lower coverage instead of counting as
#: evidence (manifest ``claim_outcome_treatment.counts_as_evidence``).
DECIDING_OBSERVATION_TYPES: frozenset[str] = frozenset(
    {"verified", "fulfilled", "contradicted", "mismatch_return", "severe_policy"}
)


def _resolve_observation_weights() -> dict[str, float]:
    """The live weight table: the manifest's numbers, else the published transcription.

    ``manifest.observation_weights`` carries a ``comment`` string alongside the numbers, so
    non-numeric entries are dropped rather than coerced — ``float("DESIGN ...")`` would turn a
    documentation string into a crash at import time.
    """
    published = dict(PUBLISHED_OBSERVATION_WEIGHTS)
    weights: dict[str, float] = {}
    for key, value in manifest_mapping("observation_weights").items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        weights[str(key)] = float(value)
    if not weights:
        return published
    # Any type the manifest does not weight keeps its published weight, so a manifest that
    # publishes a subset cannot silently un-weight the rest of the vocabulary.
    return {**published, **weights}


#: The live observation weights. ``score`` reads exactly this table.
OBSERVATION_WEIGHTS: Mapping[str, float] = MappingProxyType(_resolve_observation_weights())

#: The neutral prior. ``manifest.trust_prior`` publishes Beta(2, 2) with explicit low
#: confidence: a new store is not "probably fine", it is "unknown".
_PRIOR = manifest_mapping("trust_prior")
PRIOR_ALPHA = float(_PRIOR.get("alpha", 2.0) or 2.0)
PRIOR_BETA = float(_PRIOR.get("beta", 2.0) or 2.0)

#: Evidence half-life in days. An observation two half-lives old counts a quarter.
HALF_LIFE_DAYS = manifest_number("half_life_days", 30.0)

#: The published score below which a store is blacklisted (S2). Read, never chosen here.
BLACKLIST_THRESHOLD = manifest_number("blacklist_threshold", 0.35)

#: Clean episodes below which a store is "low data" for the exchange's exploration slice.
NEW_STORE_PRIOR_N = manifest_int("new_store_prior_n", 5)

#: Confidence at the prior: low, but never zero — a zero would make every downstream
#: multiplication by confidence collapse, and "we know nothing" is not "this is impossible".
CONFIDENCE_FLOOR = 0.05

#: Deciding evidence mass at which half the remaining confidence has been earned. Chosen so a
#: store with one clean episode across all six dimensions is visibly more credible than a new
#: one, and a store with twenty is not yet certain.
CONFIDENCE_EVIDENCE_HALF_LIFE = 12.0

_SECONDS_PER_DAY = 86400.0


def _parse_instant(value: Any) -> datetime | None:
    """An RFC-3339/ISO-8601 instant as an aware :class:`datetime`, or ``None``.

    ``None`` means "no recorded time", which the caller treats as "contemporaneous with
    ``as_of``" — an observation with no timestamp is not decayed by a guess.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"trust observation timestamp {value!r} is not an RFC-3339 instant. Decay is a "
            "function of recorded time (D17/S3), so an unparseable timestamp is refused "
            "rather than silently treated as 'now' — that would make the replay differ from "
            "the serve for reasons no assertion could name."
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rfc3339(moment: datetime) -> str:
    """The canonical spelling this package records ``decayed_at`` in."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def decay_factor(observed_at: Any, as_of: Any, *, half_life_days: float = HALF_LIFE_DAYS) -> float:
    """How much of an observation's weight survives to ``as_of``.

    ``0.5 ** (age_days / half_life_days)``, floored at an age of zero so an observation
    recorded *after* the reference instant cannot count for MORE than a fresh one — a clock
    skew on one writer must not be able to inflate a store's score.

    An observation contemporaneous with ``as_of`` decays by exactly ``1.0``: not
    approximately, exactly, which is what lets ``contradicted`` add exactly ``2.0`` to a
    dimension's beta and lets the replay assertion compare with ``==``.
    """
    if half_life_days <= 0.0:
        return 1.0
    observed = _parse_instant(observed_at)
    reference = _parse_instant(as_of)
    if observed is None or reference is None:
        return 1.0
    age_days = (reference - observed).total_seconds() / _SECONDS_PER_DAY
    if age_days <= 0.0:
        return 1.0
    return float(0.5 ** (age_days / half_life_days))


def _observation_weight(observation_type: str) -> float:
    try:
        return float(OBSERVATION_WEIGHTS[observation_type])
    except KeyError:
        raise UnknownObservationType(
            f"observation type {observation_type!r} has no published weight (known: "
            f"{sorted(OBSERVATION_WEIGHTS)}). The weights are human-approved manifest "
            "constants, not trust-engine config (D18), so an unknown type is a manifest gap "
            "— dropping it would score a dishonest behaviour as a clean record."
        ) from None


def _field(record: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a mapping or an object. Observations arrive as both."""
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _blank_dimension(as_of_text: str) -> dict[str, Any]:
    return {
        "alpha": PRIOR_ALPHA,
        "beta": PRIOR_BETA,
        "decayed_at": as_of_text,
        "coverage": 0.0,
        "observations": 0,
        "decided": 0,
        "evidence": 0.0,
    }


def _confidence(evidence_mass: float, decided_mass: float, total_mass: float) -> float:
    """Confidence in the served score: how much decided evidence there is, and its share.

    Two independent things have to be true before a score means much — that a fair amount of
    evidence exists, and that the evidence actually decided something — so confidence is the
    product of both, lifted off :data:`CONFIDENCE_FLOOR`. A store with a hundred ``ambiguous``
    outcomes has plenty of observations and no confidence, which is the point.
    """
    if evidence_mass <= 0.0 or total_mass <= 0.0:
        return CONFIDENCE_FLOOR
    saturation = 1.0 - 0.5 ** (evidence_mass / CONFIDENCE_EVIDENCE_HALF_LIFE)
    coverage = decided_mass / total_mass
    return CONFIDENCE_FLOOR + (1.0 - CONFIDENCE_FLOOR) * saturation * coverage


def prior_snapshot(*, as_of: Any) -> dict[str, Any]:
    """The snapshot a store with no observations at all is served."""
    return score((), as_of=as_of)


def score(observations: Iterable[Any], *, as_of: Any) -> dict[str, Any]:
    """Fold trust observations into one served snapshot.

    Args:
        observations: ``{store_id, dim, type, observed_at}`` records — mappings or objects —
            in the order they were recorded. Order is preserved through the fold so that two
            paths over the same stream produce bit-identical floats (S3). ``store_id`` is not
            read here: grouping by store is the caller's job (``trust.ledger.replay`` does
            it), and a snapshot is always about one store.
        as_of: the explicit instant decay is evaluated against. Never a wall clock — a
            snapshot computed against ``datetime.now()`` cannot be reproduced, and R15/S3 ask
            for exactly that reproduction.

    Returns:
        A plain, JSON-serialisable mapping:

        * ``score`` — the mean of the six dimensions' Beta means, in ``(0, 1)``;
        * ``confidence`` — in ``[CONFIDENCE_FLOOR, 1)``; see :func:`_confidence`;
        * ``score_version`` — :data:`SCORE_VERSION`;
        * ``as_of`` / ``decayed_at`` — the reference instant, normalised;
        * ``dims`` — every one of the six, each carrying ``alpha``, ``beta``, ``decayed_at``,
          ``coverage`` and the counts behind them. All six always appear: a dimension with no
          observations is served at the prior, not omitted, so a consumer never has to
          distinguish "clean" from "absent".

    Raises:
        UnknownTrustDimension: an observation named a dimension outside the published six.
        UnknownObservationType: an observation type carries no published weight.
        ValueError: a timestamp is not an RFC-3339 instant.
    """
    reference = _parse_instant(as_of)
    as_of_text = _rfc3339(reference) if reference is not None else str(as_of)

    dims: dict[str, dict[str, Any]] = {
        name: _blank_dimension(as_of_text) for name in TRUST_DIMENSIONS
    }

    for observation in observations:
        dimension = require_trust_dimension(_field(observation, "dim"))
        observation_type = str(_field(observation, "type", ""))
        weight = _observation_weight(observation_type)
        polarity = OBSERVATION_POLARITY.get(observation_type, "negative")
        decay = decay_factor(_field(observation, "observed_at"), as_of)

        entry = dims[dimension]
        entry["observations"] = int(entry["observations"]) + 1
        entry["mass"] = float(entry.get("mass", 0.0)) + decay
        if polarity == "positive":
            entry["alpha"] = float(entry["alpha"]) + weight * decay
        elif polarity == "negative":
            entry["beta"] = float(entry["beta"]) + weight * decay
        if observation_type in DECIDING_OBSERVATION_TYPES:
            entry["decided"] = int(entry["decided"]) + 1
            entry["decided_mass"] = float(entry.get("decided_mass", 0.0)) + decay
            entry["evidence"] = float(entry["evidence"]) + weight * decay

    total_mass = 0.0
    decided_mass = 0.0
    evidence_mass = 0.0
    mean_total = 0.0
    for entry in dims.values():
        mass = float(entry.pop("mass", 0.0))
        decided = float(entry.pop("decided_mass", 0.0))
        entry["coverage"] = decided / mass if mass > 0.0 else 0.0
        total_mass += mass
        decided_mass += decided
        evidence_mass += float(entry["evidence"])
        alpha, beta = float(entry["alpha"]), float(entry["beta"])
        mean_total += alpha / (alpha + beta)

    return {
        "score": mean_total / float(len(TRUST_DIMENSIONS)),
        "confidence": _confidence(evidence_mass, decided_mass, total_mass),
        "score_version": SCORE_VERSION,
        "as_of": as_of_text,
        "decayed_at": as_of_text,
        "observations": sum(int(entry["observations"]) for entry in dims.values()),
        "effective_evidence": evidence_mass,
        "dims": dims,
    }
