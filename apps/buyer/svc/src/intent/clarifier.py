"""The clarification loop (T-071, SPEC R1).

    a shopping need -> at most THREE clarifying questions -> a structured intent

The loop is a counted, terminating machine, and both halves of that matter.

**Counted.** ``questions`` is a real list and the cap is checked against ``len()`` on every
pass. ``max_questions`` is clamped *down* to :data:`~buyer_svc.intent.models.
MAX_CLARIFYING_QUESTIONS` and can never raise the ceiling, so no caller — a route handler,
a future agent, a test — can talk the loop into a fourth question. The outcome object
re-checks the cap in its own constructor, so even a hand-built ``ClarifyOutcome`` cannot
claim four questions were asked within the rules.

**Terminating.** Every pass either closes a gap, spends a question, or breaks. A buyer who
answers nothing, answers "no idea" three times, or hands over a hundred turns all reach an
intent; none of them can spin. The three loop exits are: no gaps left, the cap reached, and
no answer left to consume.

**Auction-free by construction.** :func:`clarify` takes no auction client, no exchange
handle and no HTTP session, and neither does anything it calls. There is no argument you
could pass this function that would let it open an auction, which is why R1's "no auction
before the buyer confirms" is a property of the *shape* of this module rather than of a
branch inside it. Creating an auction lives in :mod:`buyer_svc.intent.confirmation`, behind
an explicit confirmation, and that is the only door.

The model seam
--------------
One call per round, through whatever ``.complete(prompt, system=...)`` the injected client
exposes (``packages.llm``'s ``LLMClient``; ``build_llm("buyer")`` returns the offline
double by default under D20). The system contract and the ``BUYER\\n<turns>`` prompt shape
are the ones ``packages/llm/fixtures/recorded/buyer_intent.json`` was recorded against, so
a ``RecordedLLM`` built from that fixture replays into this loop unchanged — see
``apps/buyer/svc/tests/test_intent_llm_seam.py``, which proves it rather than assuming it.

If the client raises, times out, returns prose, returns nothing, or is simply ``None``, the
loop keeps its own wording and its own extraction and still produces an intent. The model
makes the questions better; it is never what makes the loop work.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from .errors import EmptyDialogue
from .extraction import (
    GAP_BUDGET,
    GAP_CONSTRAINTS,
    GAP_USE_CASE,
    IntentDraft,
    LLMProposal,
    parse_llm_reply,
)
from .models import (
    BUDGET_BAND_UNSPECIFIED,
    MAX_CLARIFYING_QUESTIONS,
    ClarifyOutcome,
    Intent,
)

__all__ = [
    "CANNED_QUESTIONS",
    "INTENT_CONTRACT",
    "clarify",
]

#: The system contract sent with every consultation.
#:
#: Byte-for-byte the ``system`` half of ``packages/llm/fixtures/recorded/buyer_intent.json``
#: (T-014, D21). That is deliberate and it is load-bearing: ``RecordedLLM`` keys on the
#: ``(system, prompt)`` PAIR, so a reworded contract here silently stops every recorded
#: reply from matching and the loop quietly falls back to its canned wording with a green
#: suite. If this text has to change, the fixture changes with it.
#:
#: **Why it now names a reply format, which it did not.** The contract described the
#: *split* R19 wants and never said how to write it down, while
#: :func:`~buyer_svc.intent.extraction.parse_llm_reply` reads a JSON object and nothing
#: else. Measured against the live model on this stack (``LLM_PROVIDER=anthropic``,
#: ``BUYER_MODEL=claude-sonnet-5``), one call with the old contract and the turns "I want a
#: cherry wood table under $200" / "It must be cherry wood, and at least 48 inches wide"
#: came back as prose whose first block was a Markdown table reading ``| material | = |
#: cherry wood |`` and ``| width | >= | 48 in |``. The model had read the shopper
#: correctly; ``_load_json_object`` found no ``{``, the bare-question fallback needs one
#: line ending in ``?``, and the whole reply was discarded. Nothing caught it because every
#: reply in the fixture and every ``llm_script`` in the goldens is hand-authored JSON, so
#: the suite proved the parser reads JSON and nothing anywhere proved a model writes it.
INTENT_CONTRACT = (
    "INTENT CONTRACT\n"
    "Split the request into hard constraints (field/op/value, filters) and preferences "
    "(field/direction/weight, scores). Ask a clarifying question only when a constraint "
    "is unusable.\n"
    "Reply with a single JSON object and nothing else - no prose, no Markdown, no code "
    'fence. Its keys are "hard_constraints" (a list of {"field","op","value"}), '
    '"preferences" (a list of {"field","direction","weight"}) and "clarifying_question" '
    "(a string, or null when nothing needs asking).\n"
    'op is one of "eq", "lte", "gte", "in", "contains". direction is one of "maximize", '
    '"minimize", "prefer". weight is a number between 0 and 1. field is a snake_case '
    "attribute name; put a unit in the name rather than in the value, so a width in inches "
    'is "width_in". Omit a list rather than inventing an entry for it.'
)

#: How the buyer's turns are rendered into the user half of the prompt. Also the recorded
#: fixture's key shape.
PROMPT_PREFIX = "BUYER"

#: What the loop asks when the model offers nothing usable. Three phrasings per gap, so a
#: buyer who answers the same question twice is not asked it in the identical words twice.
CANNED_QUESTIONS: dict[str, tuple[str, ...]] = {
    GAP_USE_CASE: (
        "What are you shopping for, and what will you use it for?",
        "Which item should I be looking for? A couple of words is plenty.",
        "Last try on this one: name the product and I will work with whatever you give me.",
    ),
    GAP_BUDGET: (
        "What is the most you would want to spend?",
        "Roughly what budget should I stay under?",
        "A ballpark number is fine - what is the ceiling?",
    ),
    GAP_CONSTRAINTS: (
        "Are there any must-haves - a size, a colour, a material - that would rule an option out?",
        "Anything non-negotiable? A feature it has to have, or one it must not?",
        "Last one: what would make you reject an option on sight?",
    ),
}


def clarify(
    turns: Sequence[str] | str,
    llm: Any = None,
    *,
    max_questions: int = MAX_CLARIFYING_QUESTIONS,
    intent_id: str | None = None,
    now: datetime | None = None,
    system: str = INTENT_CONTRACT,
) -> ClarifyOutcome:
    """Turn a buyer's utterances into a structured intent, asking at most three questions.

    Args:
        turns: the buyer's utterances, oldest first. ``turns[0]`` is the opener; each
            later turn is read as the answer to the question that was asked before it. A
            bare ``str`` is accepted as a single utterance — iterating it character by
            character is the silent catastrophe that guard exists to prevent.
        llm: any client exposing ``complete(prompt, system=...)`` — or any callable, or
            ``None``. Consulted at most once per round and never trusted to be reachable.
        max_questions: clamped into ``0 .. MAX_CLARIFYING_QUESTIONS``. It can only ever
            LOWER the cap; R1's three is not a configurable ceiling.
        intent_id: fixed id, for tests and for replaying a dialogue deterministically.
        now: creation timestamp; defaults to the current UTC instant.
        system: the system contract sent with each consultation.

    Returns:
        A :class:`~buyer_svc.intent.models.ClarifyOutcome` whose ``questions`` is at most
        three strings, whose ``intent`` is never ``None``, and whose ``confirmed`` is
        ``False``. **No auction has been created.**

    Raises:
        EmptyDialogue: if there is not one non-empty utterance to work from. Producing an
            intent out of silence would put a query nobody typed in front of the buyer.
    """
    utterances = _utterances(turns)
    if not utterances:
        raise EmptyDialogue(
            "clarify() needs at least one non-empty buyer utterance; got "
            f"{turns!r}. There is no honest intent to derive from an empty dialogue."
        )

    cap = max(0, min(int(max_questions), MAX_CLARIFYING_QUESTIONS))
    draft = IntentDraft()
    draft.absorb(utterances[0])
    pending = list(utterances[1:])

    questions: list[str] = []
    llm_calls = 0

    while True:
        proposal, consulted = _consult(llm, draft.transcript, system)
        llm_calls += consulted
        if proposal is not None:
            draft.absorb_proposal(proposal)

        gap = draft.next_gap()
        if gap is None:
            break
        if len(questions) >= cap:
            break

        question = _question_for(gap, proposal, questions)
        questions.append(question)
        if not pending:
            # The buyer walked away mid-loop. The question was asked and stays on the
            # record; the intent is built from what they did say, with the gap named in
            # `unresolved` rather than filled in with a plausible invention.
            break
        draft.absorb(pending.pop(0), gap=gap, question=question)

    # Anything the buyer volunteered beyond the questions we asked is still theirs to say.
    for extra in pending:
        draft.absorb(extra, gap=GAP_USE_CASE)

    return ClarifyOutcome(
        intent=_build_intent(draft, intent_id=intent_id, now=now),
        questions=tuple(questions),
        answers=tuple(draft.answers),
        transcript=tuple(draft.transcript),
        # Every gap the INTENT is still missing, which is what "unresolved" has always
        # meant here and still does: a shopper who answers the budget question with "no
        # idea honestly" has answered, and the budget is still unresolved. Both facts are
        # true and they are different facts, so they get different fields rather than one
        # field with a shifting meaning — `understood` below carries the second.
        unresolved=draft.gaps(),
        llm_calls=llm_calls,
        understood=tuple(draft.understood),
        softened=tuple(draft.softened),
        dropped=tuple(draft.dropped),
    )


def _utterances(turns: Sequence[str] | str) -> tuple[str, ...]:
    """Non-empty, whitespace-normalised buyer turns."""
    if isinstance(turns, str):
        candidates: Sequence[Any] = [turns]
    elif isinstance(turns, Sequence):
        candidates = turns
    else:
        candidates = list(turns) if hasattr(turns, "__iter__") else []
    return tuple(" ".join(str(turn).split()) for turn in candidates if str(turn).strip())


def _consult(llm: Any, transcript: Sequence[str], system: str) -> tuple[LLMProposal | None, int]:
    """One model call, or none. Returns the proposal and how many calls were spent.

    Exactly one invocation per round, or zero — never two. A retry with a different
    signature would consume a second scripted reply and quietly break the "one call per
    round" property that makes a recorded dialogue reproducible; a client that will not
    take ``system=`` degrades to the loop's own wording instead, which is the failure mode
    that costs nothing.
    """
    if llm is None:
        return None, 0
    method = getattr(llm, "complete", None)
    if not callable(method):
        method = llm if callable(llm) else None
    if method is None:
        return None, 0

    prompt = "\n".join([PROMPT_PREFIX, *transcript])
    try:
        reply = method(prompt, system=system)
    except Exception:  # noqa: BLE001 - an unreachable model must not take the loop down
        return None, 1
    return parse_llm_reply(reply), 1


def _question_for(gap: str, proposal: LLMProposal | None, asked: Sequence[str]) -> str:
    """The next question for ``gap``: the model's wording when it gave one, else ours.

    Never returns a question already asked, and never returns an empty string — both are
    ways a "question" reaches the buyer as blank space while still spending one of the
    three.
    """
    if proposal is not None and proposal.question and proposal.question not in asked:
        return proposal.question
    for candidate in CANNED_QUESTIONS.get(gap, ()):
        if candidate not in asked:
            return candidate
    return f"{CANNED_QUESTIONS[gap][-1]} (question {len(asked) + 1})"


def _build_intent(draft: IntentDraft, *, intent_id: str | None, now: datetime | None) -> Intent:
    """Freeze the draft into the object the buyer is asked to confirm."""
    constraints = draft.sorted_constraints()
    preferences = draft.sorted_preferences()
    query = draft.query() or (draft.transcript[0] if draft.transcript else "")
    band = draft.budget_band or BUDGET_BAND_UNSPECIFIED
    moment = now or datetime.now(UTC)
    return Intent(
        query=query,
        budget_band=band,
        intent_id=intent_id or f"int-{uuid.uuid4().hex}",
        cluster_id=_cluster_id(query, band, draft.stated_constraints(), draft.stated_preferences()),
        created_at=moment.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        hard_constraints=constraints,
        preferences=preferences,
        category=draft.category,
    )


def _cluster_id(query: str, band: str, constraints: Any, preferences: Any) -> str:
    """A content hash over what the intent actually *asks for*.

    DESIGN's ``cluster_id`` groups equivalent intents, so it is derived from the need and
    never from the session: two buyers who ask for the same thing in the same words land
    in the same cluster, which is the whole point. ``intent_id`` stays unique per session
    precisely because these two identities are different questions.

    **Only the buyer's own terms go in**, which is why the callers pass
    ``draft.stated_constraints()`` rather than everything on the draft. A model's
    contribution is not part of "the same thing in the same words": ask twice and a live
    model may embellish differently, so hashing its output makes the cluster a function of
    sampling temperature rather than of the need. That is not hypothetical here — the
    contract now asks the model for JSON, so from this commit it really does contribute,
    where before its reply was discarded unread and the distinction cost nothing.

    It is also load-bearing for the demo, which is how a drifting hash would be noticed:
    ``apps/buyer/devstack/demo-market.json`` hardcodes two cluster ids in every envelope's
    ``pursue_clusters``, and a store agent declines any solicitation whose cluster it does
    not pursue (``DeclineReason.cluster_not_pursued``). A hash that moved with the model
    would empty the demo shortlist and read as a market failure rather than as a hash.
    """
    payload = json.dumps(
        {
            "query": query.casefold(),
            "budget_band": band,
            "hard_constraints": sorted(
                json.dumps(item.to_dict(), sort_keys=True, default=str) for item in constraints
            ),
            "preferences": sorted(
                json.dumps(item.to_dict(), sort_keys=True, default=str) for item in preferences
            ),
        },
        sort_keys=True,
    )
    return f"cl-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"
