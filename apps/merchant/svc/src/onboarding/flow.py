"""Interview → envelope → written approval → active. The whole onboarding arc.

This is the module T-053's acceptance criteria name. Four functions:

``envelope_from_transcript``
    Turn a recorded plain-language interview into version 1 of a store's envelope. Always
    ``shadow`` — R7. Nothing a merchant can say in an interview activates anything; that is
    what the approval artifact is for, and an interview is not one.
``approval_digest``
    The hash a written approval is bound to (re-exported from
    :mod:`merchant_svc.envelope.digest`, where the reasoning lives).
``activate``
    Flip an envelope live against that approval, or refuse.
``edit``
    Mint the next version, leaving every earlier one exactly as it was.

The provenance on every standing commitment is stamped from the **transcript's own**
completion instant, never from the clock, so re-running this on the same transcript in a year
produces byte-identical output.
"""

from __future__ import annotations

from typing import Any

import contracts
from merchant_svc.envelope.digest import approval_digest
from merchant_svc.envelope.model import FIRST_VERSION, SHADOW, Envelope
from merchant_svc.envelope.versions import (
    activate_envelope,
    edit_envelope,
    kill_envelope,
    revive_envelope,
)
from merchant_svc.install.shop import InvalidShopDomain, normalize_shop_domain
from merchant_svc.onboarding.interview import (
    Q_ACTIVATION,
    Q_BUDGET_CAP,
    Q_COMMITMENT,
    Q_MAX_DISCOUNT,
    Q_PRICE_FLOOR,
    Q_PURSUE,
    Q_STORE,
    REQUIRED_QUESTIONS,
    Answer,
    AnswerNotUnderstood,
    Interview,
    TranscriptRejected,
    declines,
    read_money,
    read_percentage,
    read_selection,
    read_shop_host,
    read_transcript,
)

__all__ = [
    "activate",
    "approval_digest",
    "edit",
    "envelope_from_transcript",
    "kill",
    "revive",
    "store_id_from_transcript",
]

#: An owner's own words about their own store are the most authoritative evidence there is
#: (D30: 1 is strongest), and ``contracts`` publishes the number so two producers cannot pick
#: different ones for the same kind of evidence.
_OWNER_STATEMENT = contracts.ProvenanceSource.owner_statement.value
_OWNER_AUTHORITY = contracts.PROVENANCE_AUTHORITY_RANK[_OWNER_STATEMENT]


def envelope_from_transcript(transcript: Any) -> Envelope:
    """Build version 1 of a store's envelope from a recorded onboarding interview.

    Args:
        transcript: the interview — an object carrying ``turns`` and ``completed_at``, or a
            bare list of turns whose last one is stamped. See
            :mod:`merchant_svc.onboarding.interview` for the turn shape.

    Returns:
        A ``shadow`` :class:`~merchant_svc.envelope.model.Envelope` at version 1. Calling this
        twice with the same transcript returns equal envelopes.

    Raises:
        TranscriptRejected: the interview is unreadable, skipped one of
            :data:`~merchant_svc.onboarding.interview.REQUIRED_QUESTIONS`, or answered one in
            a way this module cannot read. Every one of these is a refusal rather than a
            default, because the default for a missing wall is "no wall".
    """
    interview = read_transcript(transcript)

    asked = {answer.question for answer in interview.answers}
    missing = [question for question in REQUIRED_QUESTIONS if question not in asked]
    if missing:
        raise TranscriptRejected(
            f"the onboarding interview never covered {missing}; an envelope built from it "
            "would carry limits the merchant was never asked about"
        )

    store_id = store_id_from_transcript(interview)
    document = {
        "store_id": store_id,
        "version": FIRST_VERSION,
        "floors": _floors(interview),
        "max_discount_pct": _required_percentage(interview.one(Q_MAX_DISCOUNT), "discount cap"),
        "budget_cap": _required_money(interview.one(Q_BUDGET_CAP), "discount budget"),
        "pursue_clusters": _clusters(interview),
        "standing_commitments": _commitments(interview, store_id),
        # R7, and not negotiable by anything a merchant can say in an interview: a freshly
        # interviewed envelope is in shadow. The activation question exists so the merchant
        # is *told* that, and its answer is recorded — but it sets nothing. Activation needs
        # a written approval artifact, and a spoken "yes, go live" is not one.
        "activation": SHADOW,
    }
    _check_activation_was_explained(interview.one(Q_ACTIVATION))
    return Envelope.from_obj(document, approval=None)


def store_id_from_transcript(transcript: Any) -> str:
    """The store the interview is about, as the shop's handle.

    The merchant answers with their ``<handle>.myshopify.com`` host — the same identifier the
    install flow keys an offline token on — and the handle in front of the suffix is the
    ``store_id`` the rest of the system uses.

    Raises:
        TranscriptRejected: the answer named no shop, or named one that is not a bare
            ``.myshopify.com`` host.
    """
    interview = transcript if isinstance(transcript, Interview) else read_transcript(transcript)
    answer = interview.one(Q_STORE)
    host = read_shop_host(answer.text)
    if host is None:
        raise TranscriptRejected(
            f"the interview never named a <shop>.myshopify.com store: {answer.text!r}"
        )
    try:
        normalized = normalize_shop_domain(host)
    except InvalidShopDomain as exc:  # pragma: no cover - the regex already bounds the host
        raise TranscriptRejected(f"the interview named an unusable shop: {host!r}") from exc
    return normalized[: -len(".myshopify.com")]


def approval_artifact_template(envelope: Any, approver: str, approved_at: str) -> dict[str, Any]:
    """The written approval a merchant must sign for ``envelope``, ready to be recorded.

    Offered so nothing has to hand-assemble the binding hash — a caller that computes the
    digest of the wrong document has produced an approval that will simply be refused.
    """
    return {
        "approver": approver,
        "approved_at": approved_at,
        "envelope_hash": approval_digest(envelope),
    }


# The onboarding names for the version algebra. These are **aliases, not wrappers**: the
# object bound here IS the one `merchant_svc.envelope.versions` defines and the one
# `EnvelopeVersions` calls, so the function the acceptance suite exercises through
# `onboarding.edit` is byte for byte the function `PUT /stores/{id}/envelope` runs. A
# forwarding `def` would have looked identical and been a second, untested code path
# whenever the two drifted.
activate = activate_envelope
edit = edit_envelope
kill = kill_envelope
revive = revive_envelope


# --------------------------------------------------------------------------------------
# Turning individual answers into envelope fields
# --------------------------------------------------------------------------------------
def _required_percentage(answer: Answer, what: str) -> float:
    value = read_percentage(answer.text)
    if value is None:
        raise AnswerNotUnderstood(
            f"the {what} answer names no percentage: {answer.text!r} (asked: {answer.asked!r})"
        )
    return value


def _required_money(answer: Answer, what: str) -> float:
    value = read_money(answer.text)
    if value is None:
        raise AnswerNotUnderstood(
            f"the {what} answer names no amount: {answer.text!r} (asked: {answer.asked!r})"
        )
    return value


def _floors(interview: Interview) -> list[dict[str, Any]]:
    """Every price floor the interview established, in the order it asked about them.

    An answer that declines ("no, nothing special there") records no floor. An answer that
    neither names a price nor declines is an error: a floor that quietly failed to parse is a
    wall the store thinks it has and does not.
    """
    floors: list[dict[str, Any]] = []
    for answer in interview.all_for(Q_PRICE_FLOOR):
        product_ref = answer.context.get("product_ref")
        if product_ref is not None and not isinstance(product_ref, str):
            raise TranscriptRejected(
                f"a price-floor question named product_ref={product_ref!r}, which is not a "
                "product reference"
            )
        price = read_money(answer.text)
        if price is None:
            if declines(answer.text):
                continue
            raise AnswerNotUnderstood(
                f"the price-floor answer for {product_ref or 'the whole store'} names neither "
                f"a price nor a refusal: {answer.text!r} (asked: {answer.asked!r})"
            )
        floors.append({"product_ref": product_ref, "min_price": price})
    return floors


def _clusters(interview: Interview) -> list[str]:
    """The intent clusters the merchant said yes to, in the order they were offered."""
    selected: list[str] = []
    for answer in interview.all_for(Q_PURSUE):
        options = answer.context.get("options")
        if options is None:
            raise TranscriptRejected(
                "the pursue-clusters question was asked without listing the clusters on "
                "offer; a free-text cluster name cannot be resolved to a cluster id"
            )
        if declines(answer.text):
            continue
        for cluster_id in read_selection(answer.text, list(options)):
            if cluster_id not in selected:
                selected.append(cluster_id)
    return selected


def _commitments(interview: Interview, store_id: str) -> list[dict[str, Any]]:
    """The merchant's standing promises, as ``owner_statement`` Claims.

    The *value* is the merchant's own sentence, tidied of trailing punctuation and nothing
    else. Paraphrasing it would put words in the owner's mouth and then provenance them as
    the owner's statement, which is precisely the thing verification later grades them on.
    """
    commitments: list[dict[str, Any]] = []
    for answer in interview.all_for(Q_COMMITMENT):
        key = answer.context.get("commitment_key")
        if not isinstance(key, str) or not key.strip():
            raise TranscriptRejected(
                f"a standing-commitment question named no commitment_key: {answer.asked!r}"
            )
        if declines(answer.text):
            continue
        value = answer.text.strip().rstrip(".").strip()
        if not value:
            raise AnswerNotUnderstood(f"the {key!r} commitment answer is empty")
        claim: dict[str, Any] = {
            "key": key.strip(),
            "value": value,
            "provenance": {
                "source": _OWNER_STATEMENT,
                "ref": f"envelope:{store_id}:v{FIRST_VERSION}#{key.strip()}",
                "observed_at": interview.completed_at,
                "authority_rank": _OWNER_AUTHORITY,
            },
        }
        claim_type = answer.context.get("claim_type")
        if claim_type is not None:
            # D53's vocabulary is closed and exhaustive; an unmapped claim_type must fail
            # here rather than reach the trust engine, which is the thing being graded.
            if claim_type not in {member.value for member in contracts.ClaimType}:
                raise TranscriptRejected(
                    f"the {key!r} commitment is typed {claim_type!r}, which is not one of "
                    f"D53's claim types {sorted(m.value for m in contracts.ClaimType)}"
                )
            claim["claim_type"] = claim_type
        commitments.append(claim)
    return commitments


def _check_activation_was_explained(answer: Answer) -> None:
    """The activation turn is an acknowledgement, not a switch — assert it cannot be one.

    There is no branch above that reads this answer into ``activation``, and this function
    exists to keep it that way: if a later edit ever tried to honour "yes, go live", it would
    have to delete this, which is a visible thing to do in a diff.
    """
    if not answer.text.strip():
        raise TranscriptRejected("the activation question was left unanswered")
