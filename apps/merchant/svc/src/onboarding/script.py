"""The interview the merchant service asks, as turns a browser can render and hand back.

R6 says onboarding *runs a plain-language interview*. :mod:`merchant_svc.onboarding.interview`
already reads one; what was missing is the half that ASKS it, because the interviewer's turns
were only ever written by hand into ``fixtures/interviews/`` and replayed by a CLI.

**Why the script is served as transcript turns rather than as a form.** Every interviewer turn
in a recorded interview carries machine-readable context beside its prose: ``product_ref`` on a
floor question, ``options`` on the multi-select, ``commitment_key`` and ``claim_type`` on a
standing promise. Those are the service's vocabulary — D53's closed claim-type set, the
exchange's cluster ids, Shopify variant gids — and a browser that had to *supply* them would be
a browser inventing them. So this module emits the interviewer's turns exactly as
:func:`~merchant_svc.onboarding.interview.read_transcript` will read them back, and the client's
whole job is to show ``text``, collect a sentence, and splice a ``{"role": "merchant", "text":
…}`` turn in behind it. Nothing the merchant types is ever a cluster id.

**Where the choices come from, and what happens when there are none.** The merchant service
does not own the intent-cluster taxonomy — the exchange does — so the clusters on offer are
assembled from three real sources and never invented:

* :data:`INTENT_CLUSTERS_ENV`, the deployment's statement of the network's taxonomy;
* the clusters this store's current envelope already pursues, so re-running onboarding shows
  the merchant their own choices back;
* the clusters the exchange's own loss report names for this store.

When all three are empty the question is still asked, its ``options`` list is empty, and the
panel says which variable is unset. A silently empty multi-select would read as "this store
pursues nothing", which is a term the merchant never set.

Nothing here reads the clock. The interview's completion instant is the client's to state —
it is when the merchant finished — and everything the envelope is provenanced with is stamped
from that, never from wall time (see :func:`~merchant_svc.onboarding.interview._completed_at`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

import contracts

from .interview import (
    Q_ACTIVATION,
    Q_BUDGET_CAP,
    Q_COMMITMENT,
    Q_MAX_DISCOUNT,
    Q_PRICE_FLOOR,
    Q_PURSUE,
    Q_STORE,
    REQUIRED_QUESTIONS,
)

__all__ = [
    "INTENT_CLUSTERS_ENV",
    "SHOP_SUFFIX",
    "STANDING_COMMITMENTS",
    "cluster_options",
    "interview_script",
    "shop_domain_for",
]

#: The network's intent-cluster taxonomy, as a deployment states it. Either a JSON list of
#: ``{"cluster_id": ..., "label": ...}`` objects (or bare id strings), or a comma-separated
#: list of ids. Unset is legible, never silent: see the module docstring.
INTENT_CLUSTERS_ENV: Final[str] = "NETWORK_INTENT_CLUSTERS"

#: The suffix every store handle carries. ``store_id`` is the handle in front of it — the
#: exact inverse of :func:`~merchant_svc.onboarding.flow.store_id_from_transcript`, so the
#: shop this page offers to install is the shop the interview will name.
SHOP_SUFFIX: Final[str] = ".myshopify.com"


#: The standing promises a merchant is asked to make, and the D53 claim type each one is
#: graded as. The keys are the service's, not the merchant's: a commitment key invented in a
#: browser would reach the trust engine as a dimension nothing maps.
#:
#: Every ``claim_type`` here is checked against :class:`contracts.ClaimType` at import (below),
#: because :func:`~merchant_svc.onboarding.flow._commitments` refuses an unmapped one at
#: transcript-reading time — and discovering that from a merchant's 400 would be discovering
#: it in the worst possible place.
STANDING_COMMITMENTS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "free_returns",
        "return_policy",
        "What do you always promise on returns? Say it the way you'd say it to a customer, "
        "or just say no if you'd rather not promise anything.",
    ),
    (
        "ships_within",
        "dispatch_window",
        "And how fast do you always get an order out of the door?",
    ),
    (
        "price_match",
        "promo_eligibility",
        "Anything you promise about matching a rival's price?",
    ),
)

_KNOWN_CLAIM_TYPES: Final[frozenset[str]] = frozenset(
    member.value for member in contracts.ClaimType
)
_UNMAPPED = sorted(
    claim_type for _, claim_type, _ in STANDING_COMMITMENTS if claim_type not in _KNOWN_CLAIM_TYPES
)
if _UNMAPPED:  # pragma: no cover - a wrong constant fails the import, not a merchant's request
    raise RuntimeError(
        f"the onboarding script asks for commitments typed {_UNMAPPED}, which are not in D53's "
        f"claim-type vocabulary {sorted(_KNOWN_CLAIM_TYPES)}"
    )


def shop_domain_for(store_id: str) -> str:
    """The ``<handle>.myshopify.com`` host a ``store_id`` names."""
    handle = str(store_id).strip().lower()
    if handle.endswith(SHOP_SUFFIX):
        return handle
    return f"{handle}{SHOP_SUFFIX}"


def _configured_clusters(environ: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """The deployment's cluster taxonomy, in either spelling it may be written in.

    A malformed value is treated as *stated nothing* rather than raising: this feeds a panel
    on a merchant's page, and a typo'd environment variable must degrade to "the taxonomy is
    not configured" — which the panel names — instead of 500-ing the whole dashboard read.
    """
    raw = (environ if environ is not None else os.environ).get(INTENT_CLUSTERS_ENV, "").strip()
    if not raw:
        return []
    parsed: Any = None
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
    else:
        parsed = [piece.strip() for piece in raw.split(",") if piece.strip()]
    if not isinstance(parsed, list):
        return []

    options: list[dict[str, str]] = []
    for entry in parsed:
        if isinstance(entry, str) and entry.strip():
            options.append({"cluster_id": entry.strip(), "label": _humanize(entry)})
        elif isinstance(entry, Mapping):
            identifier = entry.get("cluster_id") or entry.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                continue
            label = entry.get("label") or entry.get("name")
            options.append(
                {
                    "cluster_id": identifier.strip(),
                    "label": (label if isinstance(label, str) and label.strip() else None)
                    or _humanize(identifier),
                }
            )
    return options


def _humanize(cluster_id: str) -> str:
    """``"cluster-warm-layers"`` -> ``"warm layers"``. A label a merchant can read.

    Used only when nothing states one. The label is what the merchant's sentence is matched
    against (:func:`~merchant_svc.onboarding.interview.read_selection`), so it must be words a
    person would actually type — an id in a checkbox would force them to type the id.
    """
    text = str(cluster_id).strip().lower()
    for prefix in ("cluster-", "cluster_", "intent-", "intent_"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return " ".join(part for part in text.replace("_", " ").replace("-", " ").split()) or text


def cluster_options(
    *,
    pursued: Iterable[str] = (),
    from_reports: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Every intent cluster this store may be offered, deduplicated, in a stable order.

    Args:
        pursued: the clusters the store's current envelope already pursues.
        from_reports: cluster ids the exchange's own loss report named for this store.
        environ: where :data:`INTENT_CLUSTERS_ENV` is read from. Injectable for tests.

    Returns:
        ``[{"cluster_id": ..., "label": ...}, ...]``. Configuration first — that is the
        network's own ordering — then the store's own, then whatever its history named.
    """
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for option in _configured_clusters(environ):
        if option["cluster_id"] not in seen:
            seen.add(option["cluster_id"])
            options.append(option)

    for source in (pursued, from_reports):
        for raw in source:
            cluster_id = str(raw).strip()
            if not cluster_id or cluster_id in seen:
                continue
            seen.add(cluster_id)
            options.append({"cluster_id": cluster_id, "label": _humanize(cluster_id)})
    return options


def _floor_turns(floors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The price-floor questions: one store-wide, plus one per floor already on file.

    The store-wide turn states ``product_ref: None`` explicitly rather than omitting it, so
    the answer is filed as the whole-store floor and not as a floor for a product nobody named.
    """
    turns: list[dict[str, Any]] = [
        {
            "role": "interviewer",
            "question": Q_PRICE_FLOOR,
            "product_ref": None,
            "text": (
                "Is there a price no order should ever fall below, whatever the item is? "
                "Say no if there isn't one."
            ),
        }
    ]
    for floor in floors:
        product_ref = floor.get("product_ref")
        if not isinstance(product_ref, str) or not product_ref.strip():
            continue
        current = floor.get("min_price")
        turns.append(
            {
                "role": "interviewer",
                "question": Q_PRICE_FLOOR,
                "product_ref": product_ref,
                "text": (
                    f"And {product_ref} — you currently never let one go below {current}. "
                    "What's the lowest you'd allow now?"
                ),
            }
        )
    return turns


def interview_script(
    store_id: str,
    *,
    envelope: Mapping[str, Any] | None = None,
    report_clusters: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The whole interview for ``store_id``, as turns and as a rendering plan.

    Args:
        store_id: the store being onboarded.
        envelope: the store's current envelope document, when it has one. Used only to show
            the merchant their existing choices back — the answers still set the terms.
        report_clusters: cluster ids the exchange named for this store.
        environ: where :data:`INTENT_CLUSTERS_ENV` is read from.

    Returns:
        ``{"turns": [...], "questions": [...], "clusters_configured": bool,
        "options_offered": bool, "missing": [...]}``.

        ``turns`` is the interviewer's side of a transcript, in order, in exactly the shape
        :func:`~merchant_svc.onboarding.interview.read_transcript` reads. A client renders each
        ``text``, collects one sentence, and inserts ``{"role": "merchant", "text": …}`` after
        it; the result, plus a ``completed_at``, is a transcript this service accepts at
        ``PUT /stores/{store_id}/envelope``.

        ``questions`` is the same list with the rendering hints a form needs (what kind of
        answer is expected, whether "no" is an acceptable answer) kept OUT of the turns, so a
        transcript built from ``turns`` carries no field the interview reader does not use.
    """
    document: Mapping[str, Any] = envelope or {}
    floors = [floor for floor in document.get("floors") or () if isinstance(floor, Mapping)]
    # TWO different facts, and conflating them hid an operator's typo. `options` is what the
    # merchant can choose from; `taxonomy_stated` is whether the DEPLOYMENT said anything. A
    # store with history fills the first from its own loss report even when the second is a
    # malformed environment variable — measured, by this module's own adversarial test — and a
    # panel keyed on "are there options" then reported everything as fine while the network's
    # taxonomy was unreadable. The variable is named on the second fact, never the first.
    taxonomy_stated = bool(_configured_clusters(environ))
    options = cluster_options(
        pursued=[str(cluster) for cluster in document.get("pursue_clusters") or ()],
        from_reports=report_clusters,
        environ=environ,
    )

    turns: list[dict[str, Any]] = [
        {
            "role": "interviewer",
            "text": (
                "This takes about five minutes. I'll ask a few questions in plain English and "
                "turn your answers into the rules your agent has to stay inside. Nothing goes "
                "live until you've approved it in writing, and you can change any of it later."
            ),
        },
        {
            "role": "interviewer",
            "question": Q_STORE,
            "text": (
                f"First, which shop are we setting up? We think it's {shop_domain_for(store_id)} "
                "— say it back if that's right."
            ),
        },
        {
            "role": "interviewer",
            "question": Q_MAX_DISCOUNT,
            "text": (
                "When a shopper's agent asks you for a better price, what's the deepest "
                "discount you would ever authorise?"
            ),
        },
        {
            "role": "interviewer",
            "question": Q_BUDGET_CAP,
            "text": (
                "And across a whole month, how much are you willing to give away in discounts "
                "all told?"
            ),
        },
        *_floor_turns(floors),
        {
            "role": "interviewer",
            "question": Q_PURSUE,
            "options": options,
            "text": _pursue_prompt(options),
        },
        *[
            {
                "role": "interviewer",
                "question": Q_COMMITMENT,
                "commitment_key": key,
                "claim_type": claim_type,
                "text": prompt,
            }
            for key, claim_type, prompt in STANDING_COMMITMENTS
        ],
        {
            "role": "interviewer",
            "question": Q_ACTIVATION,
            "text": (
                "Last one. This envelope starts in shadow mode: your agent rehearses against "
                "real auctions but nothing it says reaches a shopper until you've signed off "
                "in writing. Understood?"
            ),
        },
    ]

    return {
        "turns": turns,
        "questions": [_hint(turn) for turn in turns if turn.get("question")],
        "required_questions": list(REQUIRED_QUESTIONS),
        # `clusters_configured` says what its name says: the deployment stated a taxonomy.
        # `options_offered` says whether the question has anything on it at all.
        "clusters_configured": taxonomy_stated,
        "options_offered": bool(options),
        "missing": [] if taxonomy_stated else [INTENT_CLUSTERS_ENV],
        "completed_at": (
            "the client states when the merchant finished, ISO-8601 with a timezone. Every "
            "commitment the interview records is provenanced from it, never from the clock, so "
            "the same transcript produces the same envelope forever."
        ),
    }


def _pursue_prompt(options: Sequence[Mapping[str, str]]) -> str:
    if not options:
        return (
            "Which kinds of shopper should your agent go after? This deployment states no "
            f"intent clusters ({INTENT_CLUSTERS_ENV} is unset), so there is nothing to choose "
            "from yet — say no and we'll add them when the network publishes them."
        )
    labels = ", ".join(option["label"] for option in options)
    return f"Which kinds of shopper should your agent go after — {labels}?"


#: What sort of sentence each question wants, and whether "no" is one of the answers. Kept
#: out of the turns on purpose: a transcript turn carries only fields the interview reader
#: uses, and everything else it carries lands in the answer's ``context``.
_HINTS: Final[dict[str, tuple[str, bool]]] = {
    Q_STORE: ("the shop's <handle>.myshopify.com address", False),
    Q_MAX_DISCOUNT: ("a percentage, in digits or in words", False),
    Q_BUDGET_CAP: ("an amount of money", False),
    Q_PRICE_FLOOR: ("an amount of money, or no", True),
    Q_PURSUE: ("the ones you want, in your own words; say what you don't want too", True),
    Q_COMMITMENT: ("the promise in your own words, or no", True),
    Q_ACTIVATION: ("an acknowledgement — this question sets nothing", False),
}


def _hint(turn: Mapping[str, Any]) -> dict[str, Any]:
    question = str(turn["question"])
    answer_kind, may_decline = _HINTS[question]
    hint: dict[str, Any] = {
        "question": question,
        "prompt": turn["text"],
        "answer": answer_kind,
        "may_decline": may_decline,
    }
    for carried in ("product_ref", "commitment_key", "claim_type", "options"):
        if carried in turn:
            hint[carried] = turn[carried]
    return hint
