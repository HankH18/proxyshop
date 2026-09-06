"""Every way the clarification loop refuses (T-071, SPEC R1).

All of them derive from :class:`IntentError`, and :class:`IntentError` derives from
``RuntimeError`` **on purpose**.

A refusal here is a *domain outcome* — "the buyer has not confirmed yet", "that intent has
no budget band", "this intent was already turned into an auction". It is never a report
that the caller got the call wrong. Spelling those as ``TypeError``/``ValueError``/
``AttributeError`` would put them in the same bucket as a genuine programming mistake, and
the one caller that matters most cannot tell the two apart: the R1 contract says a
``confirm(...)`` that refuses an unconfirmed intent *may* raise, but that a
``TypeError``/``AttributeError``/``ImportError``/``NameError`` out of the same call is a
**broken** implementation rather than a refusal. So refusals get their own tree, and the
builtin exception types stay reserved for real defects.
"""

from __future__ import annotations

__all__ = [
    "AuctionClientUnusable",
    "ConfirmationWithheld",
    "EmptyDialogue",
    "IntentAlreadyConfirmed",
    "IntentError",
    "IntentTooLarge",
    "InvalidConstraint",
    "InvalidPreference",
    "UnstructuredIntent",
]


class IntentError(RuntimeError):
    """Base class for every refusal from the buyer intent package."""


class EmptyDialogue(IntentError):
    """``clarify`` was handed no buyer utterance to clarify."""


class InvalidConstraint(IntentError):
    """A hard constraint that R19 cannot express as an eligibility filter."""


class InvalidPreference(IntentError):
    """A preference that R19 cannot express as a score term."""


class UnstructuredIntent(IntentError):
    """An intent missing the structure R1 requires the buyer to have confirmed."""


class IntentTooLarge(IntentError):
    """The intent is larger than this service is willing to store (T-368).

    A size refusal, and deliberately **not** an :class:`UnstructuredIntent`: the body is
    perfectly well structured, it is simply bigger than a shopping need has any reason to
    be. ``POST /buyer/intent/confirm`` takes no credential and what it accepts it KEEPS —
    the ``intent_id`` becomes a key in a process-global ledger with no capacity, no TTL and
    no sweep — so "how large may this be?" is a question with its own answer and its own
    refusal, which the route can then answer as a 413 rather than as a 422 about structure.

    Every message raised with this class names the FIELD and the CEILING and never the
    value. Quoting a 30 000-character field back at the caller makes refusing cost what
    accepting cost, which is half of the amplifier the ceiling exists to remove.
    """


class ConfirmationWithheld(IntentError):
    """The buyer has not confirmed, so no auction may be created (R1).

    This is the one exception that guards the side effect. It is raised **before** the
    auction client is so much as looked up, so a caller cannot arrange for the refusal
    path to have touched the exchange.
    """


class IntentAlreadyConfirmed(IntentError):
    """This intent already created an auction; a second confirmation is refused.

    One buyer confirmation means one auction. Without this, a double-submitted confirm
    button opens two auctions for one need, both of which solicit bids and one of which
    nobody is ever shown.
    """


class AuctionClientUnusable(IntentError):
    """The confirmed intent has nowhere to go: the auction client exposes no way in."""
