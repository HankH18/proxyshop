"""The arm a store's policy plays: **pitch variant x commitment set x discount depth** (R17).

SPEC R17 used to read "(discount depth x commitment set)" and this package was built as a
discount tuner. It now reads "(pitch variant x commitment set x discount depth)", and S4 asserts
a store agent's *pitch-variant distribution* shifts with its own win/loss record, "with discount
depth as one axis of that policy rather than its whole content". This module is that vocabulary,
held in one place so the loop, the hooks and the pitch cannot drift into three spellings of an
arm.

**What a pitch variant is, and what it is not.** It is which class of true fact the advocate
leads with. It is NOT a fact, a number, or a licence to say anything new: every variant reorders
material the bid already carries and already proved (D55 — "emphasis, ordering, framing and
which true facts to lead with, which is most of persuasion"). The vocabulary is a closed tuple
defined here in code, so a `learned_policy` off disk, a context off a queue, or a model reply
cannot introduce a fourth; :func:`variant_or_default` is the only way a string becomes one.

**What the commitment set is, and the wall it does not open.** It is which of the merchant's
*already approved* standing commitments the advocate stands behind in the pitch. It never
decides which commitments the OFFER carries — those come from `hooks.get_owner_commitments` and
are the merchant's approval, not the loop's choice. A learned policy that could drop an approved
commitment from an offer would be a policy that can edit an envelope, which is the one thing the
hook boundary exists to forbid. So a commitment the loop declines to lead with is still honoured;
it is just not the sentence the shopper reads first.

**Why depth stays here at all.** ``learning.grid`` is not a depth ladder — it is this codebase's
only fraction<->percent conversion, guarding a failure its own header calls "never refused, never
logged, and never noticed". Depth remains an axis of the arm; what it stopped being is the *only*
axis.

Nothing in this module reads a clock, a network or a random source: it is a vocabulary and four
pure functions, so it is safe for :mod:`store_agent.runtime.pitch` to import (the runtime is
under a static no-clock/no-RNG scan, and this module is what keeps the variant names from having
to be duplicated on the other side of that line).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

__all__ = [
    "DEFAULT_PITCH_VARIANT",
    "PITCH_VARIANTS",
    "VARIANT_KIND",
    "Arm",
    "arm_from_action",
    "available_commitments",
    "cold_arm",
    "commitment_keys",
    "is_variant",
    "variant_or_default",
]

#: The pitch variants a store's policy may play, in a fixed order so a Thompson draw over them
#: is reproducible. Each names what the advocate leads with:
#:
#: ``buyer_led``
#:     pure buyer relevance — whatever :func:`store_agent.runtime.pitch.rank` scored highest for
#:     THIS shopper, with no store-level emphasis on top. This is the behaviour the pitch had
#:     before a policy existed, and it is deliberately the cold arm: a store with no record of
#:     its own has learned no emphasis, and inventing one on a merchant's live bid is exactly the
#:     improvisation the hook boundary forbids.
#: ``specs_led``
#:     lead with what the product *is* — the scraped catalogue facts.
#: ``assurance_led``
#:     lead with what the merchant *promises* — the approved standing commitments.
#: ``availability_led``
#:     lead with what the feed says about *right now* — stock and units left.
PITCH_VARIANTS: tuple[str, ...] = (
    "buyer_led",
    "specs_led",
    "assurance_led",
    "availability_led",
)

#: The arm a store plays before it has a record of its own. See :data:`PITCH_VARIANTS`.
DEFAULT_PITCH_VARIANT = "buyer_led"

#: Which pitch *kind* each variant promotes. The right-hand side is
#: :data:`store_agent.runtime.pitch.KIND_ORDER`'s vocabulary, spelled here as literals rather
#: than imported, because importing it the other way round would close a cycle
#: (``runtime.pitch`` imports this module). ``test_learning_loop`` asserts the two agree, so the
#: duplication is guarded rather than trusted.
#:
#: ``buyer_led`` is absent on purpose: it promotes nothing, which is what makes it the neutral
#: arm rather than a fourth emphasis.
#:
#: A `MappingProxyType` and not a `dict`, because
#: ``test_the_learning_module_holds_no_mutable_global`` refuses a mutable container at module
#: scope in this package — R17's independence is structural, and a dict here is a dict two stores
#: in one process share. The rule is about learned STATE and this is a constant, but a scan that
#: has to distinguish the two is a scan that can be argued with; an immutable view needs no
#: argument.
VARIANT_KIND: Mapping[str, str] = MappingProxyType(
    {
        "specs_led": "catalogue",
        "assurance_led": "commitment",
        "availability_led": "availability",
    }
)


def is_variant(value: Any) -> bool:
    """Whether ``value`` is one of the published variants. String identity, nothing fuzzy."""
    return isinstance(value, str) and value in PITCH_VARIANTS


def variant_or_default(value: Any) -> str:
    """``value`` as a published variant, or :data:`DEFAULT_PITCH_VARIANT`.

    The ONLY way a string becomes a variant anywhere in this package. A `learned_policy` read off
    an operator's JSON file, a context off a queue and a claim value round-tripped through JSON
    all arrive here, and anything outside the closed vocabulary falls back to the neutral arm
    rather than being carried further as data. Fail-safe rather than fail-loud: an unrecognised
    emphasis costs the store its learned lead, and raising instead would cost it the whole bid.
    """
    return str(value) if is_variant(value) else DEFAULT_PITCH_VARIANT


def commitment_keys(value: Any) -> tuple[str, ...]:
    """A commitment set as a sorted, de-duplicated tuple of keys, whatever shape it arrived in.

    Sorted because the set rides into a provenance-tagged claim value and two runs on identical
    inputs must be byte-identical (S4); a `set` iterated in insertion order would not be.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Mapping):
        return tuple(sorted({str(k) for k in value if str(k)}))
    if isinstance(value, Iterable):
        found: set[str] = set()
        for item in value:
            key = item.get("key") if isinstance(item, Mapping) else item
            if key:
                found.add(str(key))
        return tuple(sorted(found))
    return ()


@dataclass(frozen=True, slots=True)
class Arm:
    """One play of the store's policy: which emphasis, which promises, how deep.

    Frozen and hashable, because the runner keeps one per auction it answered so that a trust
    verdict arriving later can be credited to the arm that earned it, and a mutable arm in that
    ledger would let a later auction rewrite an earlier one's record.
    """

    cluster_id: str
    pitch_variant: str = DEFAULT_PITCH_VARIANT
    commitment_set: tuple[str, ...] = ()
    depth: float = 0.0

    @property
    def kind(self) -> str:
        """The pitch kind this arm promotes, or ``""`` for the neutral arm."""
        return VARIANT_KIND.get(self.pitch_variant, "")

    def as_action(self, *, discount_pct: float) -> dict[str, Any]:
        """The arm as the `learned_policy['actions'][cluster]` mapping hook 5 reads.

        ``discount_pct`` is passed in rather than derived, because the fraction->percent
        crossing happens in exactly one place — :func:`store_agent.learning.grid.as_percent` —
        and a second conversion here is how a 20% ask becomes a 0.2% one that is inside every
        wall and therefore never refused, never logged and never noticed.
        """
        return {
            "discount_pct": float(discount_pct),
            "commitment_keys": list(self.commitment_set),
            "pitch_variant": self.pitch_variant,
        }


def cold_arm(cluster_id: str) -> Arm:
    """The arm a store with no record of its own plays: neutral emphasis, no ask.

    Both halves fail closed and only one of them is about money, which is the point. The
    envelope authorises a discount *cap*; a cap is a wall, not a mandate to spend it, so a store
    that has learned nothing asks for nothing. The emphasis half costs no money at all and still
    starts neutral, because a store that has learned nothing has learned no emphasis either.
    """
    return Arm(cluster_id=str(cluster_id))


def arm_from_action(cluster_id: str, action: Any, *, depth: float = 0.0) -> Arm:
    """Read an arm back off a rendered action mapping. Unknown variants fall to the cold one."""
    fields = action if isinstance(action, Mapping) else {}
    return Arm(
        cluster_id=str(cluster_id),
        pitch_variant=variant_or_default(fields.get("pitch_variant")),
        commitment_set=commitment_keys(fields.get("commitment_keys")),
        depth=float(depth),
    )


def available_commitments(envelope: Any) -> tuple[str, ...]:
    """The keys of the merchant's approved standing commitments, sorted.

    The arm space's commitment axis ranges over these and nothing else: a policy may choose
    which approved promises to lead with and may never mint one the merchant did not approve.
    """
    if not isinstance(envelope, Mapping):
        return ()
    standing = envelope.get("standing_commitments")
    if not isinstance(standing, Sequence) or isinstance(standing, (str, bytes)):
        return ()
    return commitment_keys(standing)
