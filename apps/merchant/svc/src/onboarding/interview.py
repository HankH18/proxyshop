"""Reading a plain-language onboarding interview.

R6 says a merchant sets their envelope by answering questions in their own words, not by
filling in a form of floats. That only works if something turns *"twenty percent off, and
that's the ceiling"* into ``max_discount_pct = 20.0`` — deterministically, offline, and with
no model in the loop, because the same transcript has to produce the same envelope on every
machine forever.

The trick that makes it tractable is that **we wrote the questions**. Each interviewer turn
carries the ``question`` it is asking and whatever the app already knows — which product a
floor is about, which commitment is being asked for, which clusters were on screen. The
merchant's side is free text; only the *answer* has to be parsed, never the topic. So this
module is a small, closed set of answer readers:

* :func:`read_percentage` — ``"20%"``, ``"twenty percent"``, ``"20 per cent"``
* :func:`read_money` — ``"$500"``, ``"500 dollars"``, ``"ninety-five dollars"``, ``"$1,200"``
* :func:`read_selection` — which of the labels on screen the merchant said yes to
* :func:`declines` — whether the answer was "no" rather than a value

Two rules keep it honest. **An answer that parses to nothing is an error, not an empty
field** — silently dropping a floor because a sentence was phrased unusually is how an
envelope ends up missing a wall. And **a label inside a negated clause is not a selection**:
"camp cooking isn't really us" must not enrol the merchant in camp cooking.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from merchant_svc.envelope.frozen import FrozenDict, freeze

# --------------------------------------------------------------------------------------
# The interview script
# --------------------------------------------------------------------------------------
Q_STORE: Final = "store"
Q_MAX_DISCOUNT: Final = "max_discount_pct"
Q_BUDGET_CAP: Final = "budget_cap"
Q_PRICE_FLOOR: Final = "price_floor"
Q_PURSUE: Final = "pursue_clusters"
Q_COMMITMENT: Final = "standing_commitment"
Q_ACTIVATION: Final = "activation"

#: Every question the onboarding interview knows how to ask. A transcript naming anything
#: else is refused rather than skipped: an answer nobody read is a term nobody set.
QUESTIONS: Final[tuple[str, ...]] = (
    Q_STORE,
    Q_MAX_DISCOUNT,
    Q_BUDGET_CAP,
    Q_PRICE_FLOOR,
    Q_PURSUE,
    Q_COMMITMENT,
    Q_ACTIVATION,
)

#: Questions that must have been *asked and answered* before an envelope exists. Every one
#: of them sets a wall, a budget, or the merchant's own promises; an interview that skipped
#: one produces an envelope with a hole in it, and a hole reads as "no limit".
REQUIRED_QUESTIONS: Final[tuple[str, ...]] = QUESTIONS

#: Who is talking. Anything else in a transcript is refused — an unattributed line means the
#: parse cannot tell a question from an answer.
INTERVIEWER_ROLES: Final[frozenset[str]] = frozenset(
    {"interviewer", "assistant", "agent", "proxyshop", "app", "system"}
)
MERCHANT_ROLES: Final[frozenset[str]] = frozenset({"merchant", "owner", "user", "human", "you"})


class TranscriptRejected(ValueError):
    """The transcript is not a readable onboarding interview."""


class AnswerNotUnderstood(TranscriptRejected):
    """A merchant's answer carried neither a value nor a refusal this module can read."""


@dataclass(frozen=True)
class Answer:
    """One merchant reply, bound to the question it answers and its context."""

    question: str
    text: str
    context: FrozenDict
    asked: str


@dataclass(frozen=True)
class Interview:
    """A completed onboarding interview, ready to be turned into an envelope."""

    answers: tuple[Answer, ...]
    completed_at: str
    interview_id: str | None = None

    def all_for(self, question: str) -> tuple[Answer, ...]:
        """Every answer to ``question``, in the order it was asked."""
        return tuple(answer for answer in self.answers if answer.question == question)

    def one(self, question: str) -> Answer:
        """The single answer to ``question``.

        Raises:
            TranscriptRejected: it was never asked, or asked more than once — two answers to
                "what is your discount ceiling?" is a transcript nobody can resolve, and
                picking one silently is picking the merchant's limits for them.
        """
        found = self.all_for(question)
        if not found:
            raise TranscriptRejected(f"the interview never asked {question!r}")
        if len(found) > 1:
            raise TranscriptRejected(
                f"the interview answered {question!r} {len(found)} times and nothing here "
                "can decide which answer is the merchant's"
            )
        return found[0]


# --------------------------------------------------------------------------------------
# Reading a transcript
# --------------------------------------------------------------------------------------
_TRANSCRIPT_KEYS: Final[tuple[str, ...]] = ("turns", "transcript", "messages", "dialogue")
_COMPLETED_KEYS: Final[tuple[str, ...]] = ("completed_at", "finished_at", "ended_at")
_TEXT_KEYS: Final[tuple[str, ...]] = ("text", "content", "message", "utterance")
_RESERVED_TURN_KEYS: Final[frozenset[str]] = frozenset(
    {"role", "speaker", "question", "at", "timestamp", *_TEXT_KEYS}
)


def read_transcript(transcript: Any) -> Interview:
    """Parse a recorded interview into the answers it contains.

    Args:
        transcript: either the interview object — ``{"completed_at": ..., "turns": [...]}`` —
            or a bare sequence of turns, in which case the last turn's ``at`` supplies the
            completion instant.

    Returns:
        The :class:`Interview`.

    Raises:
        TranscriptRejected: the transcript is not a sequence of role-tagged turns, an
            interviewer turn names a question this script does not have, an answer arrives
            with no question in front of it, a question is left unanswered, or nothing in the
            document says when the interview finished.
    """
    header: Mapping[str, Any] = {}
    if isinstance(transcript, Mapping):
        header = transcript
        turns = next(
            (transcript[key] for key in _TRANSCRIPT_KEYS if transcript.get(key)),
            None,
        )
        if turns is None:
            raise TranscriptRejected(
                f"the transcript carries none of {list(_TRANSCRIPT_KEYS)}: {sorted(transcript)}"
            )
    else:
        turns = transcript

    if isinstance(turns, (str, bytes)) or not isinstance(turns, Sequence):
        raise TranscriptRejected(
            f"an interview transcript is a list of turns, not a {type(turns).__name__}"
        )

    answers: list[Answer] = []
    pending: Answer | None = None
    stamps: list[str] = []

    for index, raw in enumerate(turns):
        if not isinstance(raw, Mapping):
            raise TranscriptRejected(f"turn {index} is a {type(raw).__name__}, not a turn")
        role = str(raw.get("role") or raw.get("speaker") or "").strip().lower()
        text = _turn_text(raw, index)
        stamp = raw.get("at") or raw.get("timestamp")
        if isinstance(stamp, str) and stamp.strip():
            stamps.append(stamp.strip())

        if role in INTERVIEWER_ROLES:
            if pending is not None:
                raise TranscriptRejected(
                    f"the interviewer asked {pending.question!r} and moved on at turn "
                    f"{index} without an answer; an unanswered question is not a blank field"
                )
            question = raw.get("question")
            if question is None:
                # A framing remark that asks nothing is fine and carries no answer.
                continue
            question = str(question).strip()
            if question not in QUESTIONS:
                raise TranscriptRejected(
                    f"turn {index} asks {question!r}, which is not in the onboarding script "
                    f"{list(QUESTIONS)}; an answer nobody reads is a term nobody set"
                )
            context = {
                str(key): value for key, value in raw.items() if str(key) not in _RESERVED_TURN_KEYS
            }
            pending = Answer(question=question, text="", context=freeze(context), asked=text)
        elif role in MERCHANT_ROLES:
            if pending is None:
                raise TranscriptRejected(
                    f"turn {index} is a merchant answer with no question in front of it: {text!r}"
                )
            answers.append(
                Answer(
                    question=pending.question,
                    text=text,
                    context=pending.context,
                    asked=pending.asked,
                )
            )
            pending = None
        else:
            raise TranscriptRejected(
                f"turn {index} is spoken by {role!r}; a transcript that cannot say who is "
                "talking cannot be read as an interview"
            )

    if pending is not None:
        raise TranscriptRejected(
            f"the interview ends on an unanswered {pending.question!r} question"
        )
    if not answers:
        raise TranscriptRejected("the transcript contains no answers")

    return Interview(
        answers=tuple(answers),
        completed_at=_completed_at(header, stamps),
        interview_id=_optional_id(header),
    )


def _turn_text(raw: Mapping[str, Any], index: int) -> str:
    for key in _TEXT_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    raise TranscriptRejected(f"turn {index} carries no text: {sorted(raw)}")


def _optional_id(header: Mapping[str, Any]) -> str | None:
    value = header.get("interview_id")
    return str(value) if isinstance(value, str) and value.strip() else None


def _completed_at(header: Mapping[str, Any], stamps: Sequence[str]) -> str:
    """When the interview finished, as an ISO-8601 UTC instant ending in ``Z``.

    Taken from the transcript, **never** from the clock. Every commitment the interview
    produces is provenanced with this instant, and a wall-clock reading would make the same
    transcript yield a different envelope on every run.
    """
    raw = next((str(header[key]) for key in _COMPLETED_KEYS if header.get(key)), None)
    if raw is None and stamps:
        raw = stamps[-1]
    if raw is None:
        raise TranscriptRejected(
            "the transcript does not say when the interview finished; the envelope's "
            "provenance is stamped from the interview, never from the clock"
        )
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise TranscriptRejected(
            f"the interview's completion instant is not ISO-8601: {raw!r}"
        ) from exc
    if moment.tzinfo is None:
        raise TranscriptRejected(f"the interview's completion instant carries no timezone: {raw!r}")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------
# Reading an answer
# --------------------------------------------------------------------------------------
_UNITS: Final[dict[str, int]] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS: Final[dict[str, int]] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

#: What a merchant says when the answer is "there isn't one". Matched against the answer's
#: **first clause only**, so "no questions asked returns for 30 days" is a commitment and
#: "no — we don't price match" is a refusal.
_DECLINES: Final[frozenset[str]] = frozenset(
    {
        "no",
        "nope",
        "none",
        "nah",
        "nothing",
        "not really",
        "no thanks",
        "no thank you",
        "we don't",
        "i don't",
        "we do not",
        "i do not",
        "there isn't one",
        "there is no",
        "no floor",
        "no cap",
        "no limit",
        "nothing like that",
        "no preference",
    }
)

#: Words that flip the clause they appear in. A cluster named inside one is NOT selected.
#: Word-bounded on purpose: a substring test would read "**not**hing" and "can**no**t" as
#: negations of whatever else was in the sentence.
_NEGATION_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:not|never|no|none|nope|nah|skip|skipping|avoid|exclude|omit|drop)\b"
    r"|n[’']t\b",
    re.IGNORECASE,
)

_CLAUSE_SPLIT: Final[re.Pattern[str]] = re.compile(
    r"[.;!?]|(?<=\s)[—–](?=\s)|,\s+(?=but\b)|\sbut\s"
)

#: The opening fragment a refusal has to fill *entirely* to count as one. Comma-delimited as
#: well as sentence-delimited, because "No, nothing special there" refuses in its first word
#: while "No questions asked returns for 30 days" is a promise that merely starts with one.
_DECLINE_HEAD: Final[re.Pattern[str]] = re.compile(r"\A\s*([^.,;:!?—–]+)")
_NUMBER: Final[str] = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_PERCENT_UNIT: Final[str] = r"(?:%|percent|per cent|pct)"
_MONEY_UNIT: Final[str] = r"(?:dollars|dollar|usd|bucks|quid|euros|euro)"

_NUMERIC_PERCENT: Final[re.Pattern[str]] = re.compile(
    rf"({_NUMBER})\s*{_PERCENT_UNIT}", re.IGNORECASE
)
_WORDED_PERCENT: Final[re.Pattern[str]] = re.compile(
    rf"((?:[A-Za-z]+[\s-]+)*[A-Za-z]+)\s+{_PERCENT_UNIT}", re.IGNORECASE
)
_DOLLAR_PREFIX: Final[re.Pattern[str]] = re.compile(rf"\$\s*({_NUMBER})")
_NUMERIC_MONEY: Final[re.Pattern[str]] = re.compile(
    rf"({_NUMBER})\s*{_MONEY_UNIT}\b", re.IGNORECASE
)
_WORDED_MONEY: Final[re.Pattern[str]] = re.compile(
    rf"((?:[A-Za-z]+[\s-]+)*[A-Za-z]+)\s+{_MONEY_UNIT}\b", re.IGNORECASE
)


def words_to_number(phrase: str) -> float | None:
    """``"ninety-five"`` → ``95.0``. ``None`` when the phrase is not a number in words.

    Handles the range a merchant actually speaks — units, teens, tens, ``hundred`` and
    ``thousand``, with or without ``and``. Anything larger or stranger is left to the digits.
    """
    tokens = [token for token in re.split(r"[\s-]+", phrase.strip().lower()) if token]
    if not tokens:
        return None
    total = 0
    current = 0
    seen = False
    for token in tokens:
        if token == "and":
            if not seen:
                return None
            continue
        if token in _UNITS:
            current += _UNITS[token]
        elif token in _TENS:
            current += _TENS[token]
        elif token == "hundred":
            current = (current or 1) * 100
        elif token == "thousand":
            total += (current or 1) * 1000
            current = 0
        else:
            return None
        seen = True
    return float(total + current) if seen else None


def _longest_number_suffix(phrase: str) -> float | None:
    """The largest trailing run of ``phrase`` that reads as a number.

    ``"nothing under forty"`` yields ``40.0``: the leading words belong to the sentence, not
    to the number, and only the tail has to parse.
    """
    tokens = [token for token in re.split(r"[\s-]+", phrase.strip()) if token]
    for start in range(len(tokens)):
        value = words_to_number(" ".join(tokens[start:]))
        if value is not None:
            return value
    return None


def _one_value(found: Sequence[float], text: str, what: str) -> float | None:
    distinct = sorted({round(value, 6) for value in found})
    if not distinct:
        return None
    if len(distinct) > 1:
        raise AnswerNotUnderstood(
            f"the answer names {len(distinct)} different {what} values {distinct} and "
            f"nothing here can choose between them: {text!r}"
        )
    return distinct[0]


def read_percentage(text: str) -> float | None:
    """The single percentage in ``text``, or ``None`` when it names none.

    Raises:
        AnswerNotUnderstood: the answer names two different percentages.
    """
    found = [float(match.group(1).replace(",", "")) for match in _NUMERIC_PERCENT.finditer(text)]
    for match in _WORDED_PERCENT.finditer(text):
        value = _longest_number_suffix(match.group(1))
        if value is not None:
            found.append(value)
    return _one_value(found, text, "percentage")


def read_money(text: str) -> float | None:
    """The single money amount in ``text``, or ``None`` when it names none.

    Raises:
        AnswerNotUnderstood: the answer names two different amounts.
    """
    found = [float(match.group(1).replace(",", "")) for match in _DOLLAR_PREFIX.finditer(text)]
    found += [float(match.group(1).replace(",", "")) for match in _NUMERIC_MONEY.finditer(text)]
    for match in _WORDED_MONEY.finditer(text):
        value = _longest_number_suffix(match.group(1))
        if value is not None:
            found.append(value)
    return _one_value(found, text, "money")


def clauses(text: str) -> list[str]:
    """``text`` split into the clauses a yes or a no can apply to independently."""
    return [clause.strip() for clause in _CLAUSE_SPLIT.split(text) if clause and clause.strip()]


def declines(text: str) -> bool:
    """``True`` when the merchant's answer is a refusal rather than a value.

    Only the **opening fragment** is considered, and it has to *be* a refusal rather than
    merely start with one. "No questions asked returns for 30 days" is a commitment; "No — we
    don't price match" is not.
    """
    match = _DECLINE_HEAD.match(text)
    if match is None:
        return False
    head = match.group(1).strip().strip("-–—…").strip().lower()
    return head in _DECLINES


def _is_negated(clause: str) -> bool:
    return _NEGATION_RE.search(clause) is not None


def read_selection(text: str, options: Sequence[Mapping[str, Any]]) -> list[str]:
    """Which of ``options`` the merchant said yes to, in the order they were offered.

    Args:
        text: the merchant's answer.
        options: what was on screen — ``[{"cluster_id": ..., "label": ...}, ...]``.

    Returns:
        The selected ids, in offer order.

    Raises:
        AnswerNotUnderstood: an option is missing its id or label.

    An option whose label appears only inside a negated clause is **not** selected, and a
    label that appears in both a plain and a negated clause is not selected either: pursuing
    a cluster the merchant declined spends their budget on their behalf.
    """
    parts = clauses(text) or [text]
    positive = " \n ".join(part.lower() for part in parts if not _is_negated(part))
    negative = " \n ".join(part.lower() for part in parts if _is_negated(part))

    selected: list[str] = []
    for option in options:
        if not isinstance(option, Mapping):
            raise AnswerNotUnderstood(f"an interview option is a {type(option).__name__}")
        identifier = option.get("cluster_id") or option.get("id") or option.get("value")
        label = option.get("label") or option.get("name")
        if not isinstance(identifier, str) or not identifier.strip():
            raise AnswerNotUnderstood(f"interview option {option!r} has no id")
        if not isinstance(label, str) or not label.strip():
            raise AnswerNotUnderstood(f"interview option {option!r} has no label")
        needle = label.strip().lower()
        if needle in positive and needle not in negative:
            selected.append(identifier.strip())
    return selected


_SHOP_HOST: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.myshopify\.com)\b"
)


def read_shop_host(text: str) -> str | None:
    """The single ``<name>.myshopify.com`` host named in ``text``, or ``None``.

    Raises:
        AnswerNotUnderstood: the answer names two different shops. The install flow keys
            everything on this host; guessing which one the merchant meant would point an
            envelope at a store that never agreed to it.
    """
    hosts = sorted({match.group(1).lower() for match in _SHOP_HOST.finditer(text)})
    if not hosts:
        return None
    if len(hosts) > 1:
        raise AnswerNotUnderstood(f"the answer names more than one shop: {hosts}")
    return hosts[0]
