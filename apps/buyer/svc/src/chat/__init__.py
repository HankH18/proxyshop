"""``buyer_svc.chat`` — the shopper's follow-up questions about the shortlist on screen.

    POST /buyer/chat/ask   {"auction_id": "...", "question": "..."}   -> 200 an answer

One route, one subject: the options this auction already put in front of this shopper. It is
not a general assistant and has no catalogue, no search and no memory — the only material it
can reach is :func:`buyer_svc.chat.evidence.corpus_for`'s reading of the exchange's own live
shortlist for one auction.

The design decision, stated once
================================
**The platform answers from what it authored, attributes what a shop claimed, quotes what a
shop wrote, and refuses everything else by name.** :mod:`buyer_svc.chat.evidence` sorts every
fact into those three voices; :mod:`buyer_svc.chat.answering` decides what a question may be
answered from and screens what a model wrote; :mod:`buyer_svc.chat.routes` fetches the
material from the exchange rather than taking it off the wire, so nothing a shopper types can
become a fact.

``routes`` is not imported here: it drags FastAPI into every consumer of :func:`answer_about`.
The frozen :func:`buyer_svc.main.create_app` imports it directly by its module path.
"""

from __future__ import annotations

from ..intent._spellings import bind_package
from .answering import (
    ANSWER_CONTRACT,
    MAX_ANSWER_CHARS,
    MAX_QUESTION_CHARS,
    NOT_HELD_DETAIL,
    SOURCE_ASSEMBLED,
    SOURCE_WRITTEN,
    ChatAnswer,
    Reading,
    SlotAnswer,
    answer_about,
    answer_prompt,
    assemble,
    compose,
    read_question,
    screen,
    screen_reasons,
)
from .evidence import (
    TOPICS,
    VOICE_PLATFORM,
    VOICE_SHOP,
    VOICE_SHOP_CLAIM,
    Corpus,
    Evidence,
    SlotEvidence,
    corpus_for,
)

__all__ = [
    "ANSWER_CONTRACT",
    "MAX_ANSWER_CHARS",
    "MAX_QUESTION_CHARS",
    "NOT_HELD_DETAIL",
    "SOURCE_ASSEMBLED",
    "SOURCE_WRITTEN",
    "TOPICS",
    "VOICE_PLATFORM",
    "VOICE_SHOP",
    "VOICE_SHOP_CLAIM",
    "ChatAnswer",
    "Corpus",
    "Evidence",
    "Reading",
    "SlotAnswer",
    "SlotEvidence",
    "answer_about",
    "answer_prompt",
    "assemble",
    "compose",
    "corpus_for",
    "read_question",
    "screen",
    "screen_reasons",
]

# Both dotted spellings of this directory must resolve to ONE module object per file. Left
# alone, Python executes every file here TWICE — once as `buyer_svc.chat`, once as
# `apps.buyer.svc.src.chat`, which is the spelling the frozen acceptance suite uses — and a
# process that reached this package both ways would hold two `Evidence` classes, so an
# `isinstance` or an `in grounds` comparison across the seam would silently disagree, and
# two copies of `FAMILY_WORDS` for a deployment to have to patch twice. This reuses
# `buyer_svc.intent._spellings` rather than adding a seventh copy of it, exactly as
# `buyer_svc.pitch` does.
bind_package(__name__)
