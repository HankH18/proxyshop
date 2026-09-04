"""The declared vocabulary an accept's ``denial_reason`` is drawn from (T-204, T-264).

``denial_reason`` is not a log line. It is written into the ``policy_event`` payload
:func:`~.offer._refusal_event` persists, and it is the body of the 409 the published contract
declares for ``POST /auctions/{auction_id}/accept``. It is therefore a **contract**, and until
this module existed its vocabulary was whatever ``type(exc).__name__`` happened to be:

* six distinct leading tokens were reachable, and nothing in the repository asserted on the
  set — the only two assertions that touched the field pinned two individual strings;
* three of the values changed inside a single branch, and nothing broke, because nothing
  would have noticed;
* renaming an exception class silently changed a published value;
* and one of the reachable values rendered a **memory address** into that persisted,
  client-visible payload — ``registered_domains <object object at 0x104e0a170> exposes
  neither domain_for(store_id) nor __call__(store_id)`` (T-264). That leaks process memory
  layout to an unauthenticated client and makes the reason unparseable downstream.

The shape, and it is deliberately the shape the field already had
-----------------------------------------------------------------

::

    <declared code>: <free prose>

The part before the first colon is the contract: one of :data:`DENIAL_REASONS`, and nothing
else. Everything after it is prose for a human, and no client should parse it — it names the
auction, the bid, the refused host, the exception class, whatever makes the refusal
investigable. Keeping prose is not a concession: a refusal an operator cannot investigate is
the one that costs a support ticket, and T-215's split (a live discount code never appears in
this string, only its fingerprint) already depends on the prose being writable.

That shape is why this module changes no message text at all. Every reason ``accept()``
emitted before is emitted byte-for-byte now; what is new is that a *declared* constant is
where its first token comes from, so the set can be asserted on and a rename cannot move it
quietly. ``exchange.accept.DENIAL_REASONS`` is the declaration, exported from the package.

Why an exported constant rather than an ``enum`` in the published OpenAPI: both are real
answers, and the document is owned by ``packages/contracts``, which this ticket may not edit.
The enum there is reported in NEEDS; ``packages/contracts/tests/test_repro_open_tickets.py::
test_the_published_denial_reason_has_an_enumerated_vocabulary`` is its gate and stays red
until that lane adds it.
"""

from __future__ import annotations

from typing import Any

from ..eligibility import BLACKLISTED, UNAVAILABLE

__all__ = [
    "DENIAL_ALREADY_ACCEPTED",
    "DENIAL_AUCTION_NOT_ACCEPTABLE",
    "DENIAL_BLACKLISTED",
    "DENIAL_CHECKOUT_REFUSED",
    "DENIAL_REASONS",
    "DENIAL_UNAVAILABLE",
    "DENIAL_UNKNOWN_BID",
    "DENIAL_UNRECORDABLE_ACCEPTANCE",
    "DENIAL_UNSPECIFIED",
    "denial_code",
    "denial_reason",
    "describe",
]

#: The separator between the declared code and the prose. One colon and one space, which is
#: what every reason in this package already used.
DENIAL_SEPARATOR = ": "

#: This auction carries no such bid, so there is nothing to accept.
DENIAL_UNKNOWN_BID = "unknown_bid"
#: The auction was already accepted; a second accept issues no second discount code (R3/A5).
DENIAL_ALREADY_ACCEPTED = "already_accepted"
#: The auction cannot record an acceptance, so a second accept could not be refused — the
#: first is refused rather than issuing a code that cannot be made single-use.
DENIAL_UNRECORDABLE_ACCEPTANCE = "unrecordable_acceptance"
#: The checkout port refused: an off-domain host, an unusable offer, a merchant that would
#: not mint. The prose names the exception class, which is diagnosis and not vocabulary.
DENIAL_CHECKOUT_REFUSED = "checkout_refused"
#: R12, re-read at accept time: the store is blacklisted. Imported, never restated (D30), so
#: the gate's status word and this vocabulary cannot drift into two spellings.
DENIAL_BLACKLISTED = BLACKLISTED
#: R12 could not be established — an unreadable source, an unsupported interface version, a
#: read that raised. Fail closed, and it denies.
DENIAL_UNAVAILABLE = UNAVAILABLE
#: The auction is in a state from which ``accepted`` is not a legal move (served route only).
DENIAL_AUCTION_NOT_ACCEPTABLE = "auction_not_acceptable"
#: The boundary's fail-safe, and the reason this vocabulary is closed rather than advisory: a
#: refusal reaching the published surface with a code nothing declares is re-published under
#: this one, its prose kept intact. A client parsing ``denial_reason`` therefore never sees a
#: code outside :data:`DENIAL_REASONS`, and the diagnosis is not thrown away to achieve that.
DENIAL_UNSPECIFIED = "unspecified"

#: **The declared vocabulary.** Every ``denial_reason`` this package produces begins with one
#: of these, followed by ``": "`` and free prose. Exported as ``exchange.accept.DENIAL_REASONS``.
DENIAL_REASONS: tuple[str, ...] = (
    DENIAL_ALREADY_ACCEPTED,
    DENIAL_AUCTION_NOT_ACCEPTABLE,
    DENIAL_BLACKLISTED,
    DENIAL_CHECKOUT_REFUSED,
    DENIAL_UNAVAILABLE,
    DENIAL_UNKNOWN_BID,
    DENIAL_UNRECORDABLE_ACCEPTANCE,
    DENIAL_UNSPECIFIED,
)

#: Values whose ``repr`` is information rather than an address. Everything else is described
#: by its type — see :func:`describe`.
_REPR_IS_SAFE: tuple[type, ...] = (str, bytes, bool, int, float, complex, type(None))


def describe(value: Any) -> str:
    """A rendering of ``value`` that is safe to persist and to publish (T-264).

    ``f"{value!r}"`` on an object with the default ``__repr__`` produces
    ``<object object at 0x104e0a170>`` — the object's address in this process. In a field
    that is written to a durable event and returned to an unauthenticated caller that is a
    memory-layout leak, and it also makes the reason unstable: the same refusal renders
    differently on every run, so nothing downstream can group two of them.

    Values whose ``repr`` carries meaning keep it; anything else is named by its type, which
    is the part an operator actually needs ("you passed a ``StaticSellerEligibility`` where a
    version string belongs").
    """
    if isinstance(value, _REPR_IS_SAFE):
        return repr(value)
    return f"<{type(value).__name__}>"


def denial_reason(code: str, detail: str = "") -> str:
    """``"<code>: <detail>"`` — the published shape, built from a declared code.

    ``code`` is asserted to be declared rather than quietly accepted: a caller inventing a
    seventh vocabulary term is the exact defect this module closes, and a wrong constant is a
    programming error, not a runtime condition to degrade around. Use
    :data:`DENIAL_UNSPECIFIED` when re-publishing a reason whose origin is not known.
    """
    if code not in DENIAL_REASONS:
        raise ValueError(
            f"{code!r} is not a declared denial reason; denial_reason is persisted into a "
            f"policy_event and published on the 409, so its vocabulary is "
            f"{list(DENIAL_REASONS)}"
        )
    text = str(detail).strip()
    return f"{code}{DENIAL_SEPARATOR}{text}" if text else code


def denial_code(reason: Any) -> str | None:
    """The declared code ``reason`` begins with, or ``None`` when it begins with none.

    ``None`` is the answer that matters: it is how the published boundary detects a refusal
    whose vocabulary it does not own and re-publishes it under :data:`DENIAL_UNSPECIFIED`
    instead of passing an undeclared token to a client.
    """
    token = str(reason or "").split(DENIAL_SEPARATOR.strip(), 1)[0].strip()
    return token if token in DENIAL_REASONS else None
