"""``buyer_svc.pitch`` — the BUYER-SIDE agent (SPEC core tenet, D55).

The organic half of the organic/sponsored split, and the half the buyer service did not have.

    >>> from buyer_svc.accept import render_shortlist
    >>> from buyer_svc.pitch import pitches_for
    >>> slots = render_shortlist(shortlist)
    >>> pitches = pitches_for(slots, shortlist["slots"], intent=intent, profile=profile)
    >>> pitches[0].platform_case
    'You are weighing price, and here it is: 78.00 USD. Also discount: 10% off; ...'
    >>> pitches[0].store_pitch          # the shop's own advocate wrote this; unedited
    'We knit these in Yorkshire and ...'
    >>> pitches[0].voices
    ('store', 'platform')

Two agents, two objective functions, and this is the one that wants the shopper to buy *a*
product: it pitches EVERY candidate as well as it honestly can, scraped shops included, since
a shop with no advocate of its own gets no other voice. What a shop buys by joining is not
visibility and not a better score — visibility is organic and earned by matching — it is the
right to make its case in its own words, which is why :attr:`~buyer_svc.pitch.writing
.SlotPitch.store_pitch` is carried byte for byte and never re-voiced.

Where each half of D55 lives:

* *may choose emphasis, ordering, framing and which true facts to lead with* —
  :func:`buyer_svc.pitch.material.rank`, from the shopper's own constraints, preferences,
  budget band and coarse buckets.
* *may not introduce a fact the platform has not checked* —
  :func:`buyer_svc.pitch.material.material_for` (the slot is the whole of the material) and
  :func:`buyer_svc.pitch.writing.screen_reasons` (every number in a generated case must
  already be in that material).
* *the sponsored message is the one with a motive* —
  :func:`buyer_svc.pitch.writing.store_pitch_of` carries it unchanged, and it never enters
  the platform's own prompt (C10: pitch content is untrusted data, never an instruction).

Imports inside this package are relative on purpose; see :mod:`buyer_svc.vault` for why (this
tree is reachable both as ``buyer_svc.pitch`` and as ``apps.buyer.svc.src.pitch``, and an
absolute import silently picks one). This package deliberately contains **no** ``routes.py``:
the pitch is served on the shortlist route a shopper already drives,
``POST /buyer/shortlist/render``, because a case nobody is shown beside the slot it argues for
is a case nobody reads.
"""

from __future__ import annotations

from ..intent._spellings import bind_package
from .material import (
    KIND_CAVEAT,
    KIND_COMMITMENT,
    KIND_PRICE,
    KIND_TRUST,
    MAX_CASE_FACTS,
    PROFILE_BUCKET_KEYS,
    CaseMaterial,
    PlatformFact,
    humanised,
    material_for,
    numeric_tokens,
    rank,
    slot_facts,
)
from .writer import PITCH_TIMEOUT_SECONDS, pitch_writer, set_pitch_writer
from .writing import (
    MAX_CASE_CHARS,
    MAX_WRITTEN_SLOTS,
    MIN_CASE_WORDS,
    PLATFORM_CONTRACT,
    SOURCE_ASSEMBLED,
    SOURCE_WRITTEN,
    UNSUPPORTABLE_WORDS,
    VOICE_PLATFORM,
    VOICE_STORE,
    SlotPitch,
    assemble,
    case_prompt,
    compose_case,
    pitch_for,
    pitches_for,
    screen,
    screen_reasons,
    store_pitch_of,
)

__all__ = [
    "KIND_CAVEAT",
    "KIND_COMMITMENT",
    "KIND_PRICE",
    "KIND_TRUST",
    "MAX_CASE_CHARS",
    "MAX_CASE_FACTS",
    "MAX_WRITTEN_SLOTS",
    "MIN_CASE_WORDS",
    "PITCH_TIMEOUT_SECONDS",
    "PLATFORM_CONTRACT",
    "PROFILE_BUCKET_KEYS",
    "SOURCE_ASSEMBLED",
    "SOURCE_WRITTEN",
    "UNSUPPORTABLE_WORDS",
    "VOICE_PLATFORM",
    "VOICE_STORE",
    "CaseMaterial",
    "PlatformFact",
    "SlotPitch",
    "assemble",
    "case_prompt",
    "compose_case",
    "humanised",
    "material_for",
    "numeric_tokens",
    "pitch_for",
    "pitch_writer",
    "pitches_for",
    "rank",
    "screen",
    "screen_reasons",
    "set_pitch_writer",
    "slot_facts",
    "store_pitch_of",
]

# Both dotted spellings of this directory must resolve to ONE module object per file, or a
# process that reaches this package under both names holds two `SlotPitch` classes and two
# copies of the writer's module-level seam — so `set_pitch_writer` installed through one
# spelling would be invisible to the render path reached through the other. See
# `buyer_svc.intent._spellings`, whose implementation this reuses rather than re-copying.
bind_package(__name__)
