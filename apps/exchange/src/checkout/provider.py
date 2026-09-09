"""``CheckoutProvider`` — the single port every accept path goes through (R3, A5, C11, D45).

A won offer becomes a discount code and a permalink in exactly one place: here. The port is
a **template method**, and the split between what the port does and what an implementation
does is the entire point of T-036:

===============================================  =====================================
the PORT does, for every provider, always        an IMPLEMENTATION does
===============================================  =====================================
validate the offer's host against the registered  :meth:`CheckoutProvider.mint` — turn an
seller domain, **before** anything is minted      approved offer into a code + permalink
validate the permalink the provider handed back
emit the three ordered `LedgerEvent` kinds
===============================================  =====================================

Two consequences, both deliberate:

**No implementation can opt out of the domain check.** It is not a rule providers are asked
to follow — :meth:`CheckoutProvider.checkout` runs it and is final:
``__init_subclass__`` refuses, at class-definition time, any subclass that overrides
``checkout``. A provider written next year by someone who never read D22 still cannot mint a
code for ``attacker.tld``.

**Nothing downstream can tell which provider ran (C11).** The events are built by the port
from the request, not by the provider, so ``redirect`` and the Shopify spellings emit the
identical ordered kinds by construction rather than by two implementations happening to
agree. D23 and `DESIGN.md:84` explicitly reject divergent event schemas per mode, and the
frozen suite asserts the equality (``test_e3_exchange.py:688``).

The Shopify path (T-052) is one adapter behind this port — not the route to a code.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from .. import describe, describe_exception, redact_addresses
from ..auction.ledger import MalformedLedgerPayload, build_published_event, record_audit_anomaly
from .codes import (
    assert_offer_is_mintable,
    build_cart_permalink,
    code_expiry,
    minting_ledger,
    offer_quantity,
)
from .domain import OffDomainCheckout, assert_on_domain
from .redaction import (
    code_fingerprint,
    redact_code,
    redact_url,
    render_can_publish,
    safe_token,
    spells_code,
)
from .validity import (
    DiscountDoesNotApply,
    assert_the_cart_applies_this_code,
    assert_the_window_is_open,
)

__all__ = [
    "CHECKOUT_EVENT_KINDS",
    "CheckoutProvider",
    "CheckoutRequest",
    "CheckoutResult",
    "MintedCheckout",
    "OrphanedCheckoutCode",
    "OrphanedCode",
    "OrphanedOffDomainCheckout",
    "OrphanedUnusableDiscount",
    "PortMethodIsFinal",
    "RedactedCause",
    "RegisteredDomains",
    "code_fingerprint",
    "default_permalink",
    "redact_code",
    "redact_url",
    "registered_domain_for",
    "safe_token",
    "spells_code",
]


class RegisteredDomains(Protocol):
    """The platform's own answer to "what domain is this seller registered at?".

    Implementations read ``app.sellers`` (or whatever holds the registration) and return
    ``None`` for a store they do not know. A callable taking ``store_id`` works too.
    """

    def domain_for(self, store_id: str) -> str | None: ...


#: C11's three frozen `LedgerEventKind` values (D24), named as values rather than only as a
#: tuple of them.
#:
#: A tuple literal is not a name: a sweep of every product module for the kinds it produces read
#: `CHECKOUT_EVENT_KINDS` and saw nothing, so `checkout_redirect` — emitted by `_events` below
#: on every minting checkout, and by `accept._handoff_events` on every fallback — was reported
#: as a frozen kind with no producer anywhere in the tree (T-302). It had one. What it did not
#: have was anywhere a reader could find it. Same convention as `ranking/serving.py`'s
#: `SHOWN_KIND` and `auction/state.py`'s `AUCTION_OPENED_KIND`.
ACCEPTED_KIND = "accepted"
CODE_CREATED_KIND = "code_created"
CHECKOUT_REDIRECT_KIND = "checkout_redirect"

#: C11: the ordered `LedgerEvent` kinds every checkout emits, whichever provider ran.
CHECKOUT_EVENT_KINDS: tuple[str, str, str] = (
    ACCEPTED_KIND,
    CODE_CREATED_KIND,
    CHECKOUT_REDIRECT_KIND,
)


class PortMethodIsFinal(TypeError):
    """A provider tried to override a method the port performs on every provider's behalf."""


# T-215 pass 3 — the redaction primitives now live in `redaction.py`, BELOW this module and
# below `domain`, because `domain` needs them too. Two fixes redacted only at THIS module's
# boundary (`OrphanedCheckoutCode.__init__`), and both were defeated by prose built inside
# `domain` out of a merchant value this module never held. They are re-exported here under
# their historical names, so every existing import keeps working.


def _rebuild_orphaned(
    kind: type[OrphanedCheckoutCode], args: tuple[Any, ...], orphan: OrphanedCode
) -> OrphanedCheckoutCode:
    """Module-level so :meth:`OrphanedCheckoutCode.__reduce__` is picklable."""
    return kind(*args, orphan=orphan)


#: The C-level ``__cause__``/``__context__`` slots, reached past the Python-level properties
#: :class:`OrphanedCheckoutCode` puts in front of them. ``raise X from Y`` writes the slot
#: directly (``PyException_SetCause``), so a subclass cannot intercept the *write* — but every
#: reader, :mod:`traceback` included, goes through ordinary attribute lookup, and that is the
#: hook these give us.
_BASE_CAUSE = BaseException.__dict__["__cause__"]
_BASE_CONTEXT = BaseException.__dict__["__context__"]

#: How many exception objects one in-place redaction pass will visit. A chain is short in
#: practice; an ``ExceptionGroup`` a merchant's client library builds need not be, and this
#: runs on a refusal path. Exceeding it costs only the best-effort rewrite — the guarantee
#: is :func:`_sanitised_cause`, which drops the whole object when the RENDER still spells
#: the code, and does not depend on this walk having reached everything.
_MAX_CHAIN_NODES = 256

#: Where :meth:`OrphanedCheckoutCode._sanitised_slot` remembers what it decided about a
#: chained slot, in the instance ``__dict__``. Keyed by slot name (``"__cause__"`` /
#: ``"__context__"``); the value is ``(the object that was in the slot, the object the
#: decision produced)`` or :data:`_DECIDING` while the decision is being taken.
_SLOT_DECISIONS = "_psx_slot_decisions"

#: In the slot-decision table: "this exact decision is already running further up the
#: stack." Reading a redacting slot RENDERS, and rendering READS the slot again, so the
#: decision is genuinely re-entrant; see :meth:`OrphanedCheckoutCode._sanitised_slot`.
_DECIDING = object()


def _redact_chain(exc: BaseException | None, code: str, urls: Sequence[str] = ()) -> None:
    """Redact ``code`` and ``urls`` out of ``exc`` and every exception it chains to, in place.

    The exception that *caused* an orphan refusal is the one that named the offending URL —
    and a cart permalink spells the discount in its ``?discount=``. Redacting only the
    orphan's own message leaves that spelling one ``traceback.format_exc()`` away, which is
    precisely how an exception reaches a log file. Rewriting ``args`` is what actually moves
    the needle: it is what ``str()``, ``repr()`` and the traceback's final line all render.

    **Called eagerly at every ``raise … from`` site in this package — through
    :func:`_sanitised_cause` — not only lazily from the properties below.** The properties
    are a Python-level attribute lookup, and the interpreter's default excepthook is not:
    C-level ``PyErr_Display`` walks the chain through ``PyException_GetCause``, reads the C
    slot directly, and never performs the lookup a ``property`` depends on — measured, it
    printed ``?discount=PSX-SECRET-ABC123`` in full. Mutating ``args`` in place is what
    closes that channel, because it changes the object the C slot points AT rather than what
    a Python reader sees. It is written to be idempotent so that running eagerly and again
    lazily cannot corrupt the message.

    Rewriting ``args`` is necessary and, since pass 3, known not to be sufficient: an
    exception whose ``__str__`` ignores ``args`` renders from its own fields and is
    untouched by anything here. :func:`_sanitised_cause` is what handles that, by not
    chaining such an object at all.

    **What this walk covers, and why it walks more than the chain (pass 4).** ``args`` is
    not the only thing a renderer prints. PEP-678 ``__notes__`` are printed on their own
    lines by ``format_exception_only``; an ``ExceptionGroup``'s members are printed under
    the ``+-+---`` rules and are reachable only through ``.exceptions``, which is neither
    ``__cause__`` nor ``__context__``. Both are rewritten in place here so that a note or a
    member that merely quotes a permalink is REDUCED and survives as a diagnostic, instead
    of costing the whole cause its place in the chain.

    This is still the best-effort half. The guarantee is :func:`_sanitised_cause`, which
    asks the renderer itself whether anything is left and drops the entire object when it
    is — so a form this walk cannot reach is covered without this walk having to name it.
    """
    seen: set[int] = set()
    frontier: list[BaseException] = [exc] if exc is not None else []
    while frontier and len(seen) < _MAX_CHAIN_NODES:
        node = frontier.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        _redact_node(node, code, urls)

        # `ExceptionGroup.exceptions` — members the renderer prints and the chain omits.
        try:
            members = getattr(node, "exceptions", None)
        except Exception:
            members = None
        if isinstance(members, tuple):
            frontier.extend(m for m in members if isinstance(m, BaseException))

        for nxt in (_BASE_CAUSE.__get__(node), _BASE_CONTEXT.__get__(node)):
            if nxt is not None:
                frontier.append(nxt)


def _redact_node(node: BaseException, code: str, urls: Sequence[str]) -> None:
    """Rewrite one exception's ``args`` and ``__notes__`` in place. **Cannot raise.**

    Every line here touches an object a merchant's library authored, and each of the four
    ways that can bite was measured on this tree rather than imagined:

    * ``str(arg)`` on an argument whose ``__str__`` raises. Now inside
      :func:`~.redaction.spells_code`, which fails closed;
    * ``redacted != node.args`` — a tuple comparison runs the *elements'* ``__eq__``, so an
      argument with a hostile ``__eq__`` made the comparison raise. It is an IDENTITY test
      now: :func:`_redact_arg` returns the argument itself or a fresh string, so ``is not``
      answers "did anything change?" exactly, and identity cannot be overridden;
    * ``__notes__`` is not always a plain list — ``LazilyNotedFailure`` in the gate makes it
      a *property*, and a property can raise or return a fresh list each read (in which case
      writing into it edits a temporary and the object still renders the original); and
    * assigning ``node.args`` at all, which a class with a custom ``args`` setter can refuse.

    A node this fails on is left as it was, and that is safe *because this walk is the
    best-effort half*: the guarantee is :func:`_sanitised_cause`, which asks whether any
    renderer can still publish and substitutes the whole object when the answer is "yes, or
    I cannot tell". Raising here instead skipped that guarantee entirely — the
    ``OrphanedCheckoutCode`` was never constructed, so ``accept()`` filed no ``code_created``
    event and the live discount became unrevokable, which is T-202 restored by the very code
    written to close T-215.
    """
    try:
        args = node.args
        redacted = tuple(_redact_arg(arg, code, urls) for arg in args)
        if any(new is not old for new, old in zip(redacted, args, strict=True)):
            node.args = redacted
    except Exception:
        pass

    # PEP-678 notes: a separate list, printed by the renderer, absent from `str()`.
    try:
        notes = getattr(node, "__notes__", None)
        if isinstance(notes, list):
            for index, note in enumerate(notes):
                cleaned = _redact_arg(note, code, urls)
                if cleaned is not note:
                    notes[index] = cleaned
    except Exception:
        pass


def _redact_arg(arg: Any, code: str, urls: Sequence[str]) -> Any:
    """One argument of one chained exception, redacted and then checked — T-215 pass 3.

    ``redact_code`` is best-effort by construction: its structural layer removes the URLs
    this package HOLDS, and its literal layer removes spellings of the code this package
    FORMATTED. A chained cause is neither. It was built by whatever raised it — ``codes.py``
    quoting a merchant ``expires_at``, a third-party library quoting whatever it was handed —
    so a spelling neither layer can reach walks into ``traceback.format_exception`` and the
    C-level excepthook, which read these ``args`` directly.

    So the redaction is followed by a CHECK, and the check fails closed: an argument that
    still yields a redeemable code after the best effort is dropped whole rather than
    published. Losing one chained message costs a diagnostic; publishing it costs a discount
    nobody can revoke. Idempotent — the placeholder does not spell the code, so a second
    pass leaves it alone.
    """
    if isinstance(arg, str):
        return safe_token(redact_code(arg, code, urls=urls), code, label="chained-message")
    # A non-string argument still renders into `str(exc)`, so it is checked too — but only
    # REPLACED when it actually spells the code, because rewriting an unrelated library's
    # argument tuple is a change with its own blast radius.
    #
    # The object goes in WHOLE. This read `spells_code(str(arg), code)` until pass 6, and
    # that `str(...)` sat outside every guard in this package: an argument whose `__str__`
    # raises took the sanitiser down with it. `spells_code` converts inside its own `try` and
    # answers "yes, redact" for anything it cannot read, so an unreadable argument is now
    # dropped rather than being the thing that destroys the refusal.
    if code and spells_code(arg, code):
        return f"<redacted-chained-arg:{code_fingerprint(code)}>"
    return arg


class RedactedCause(Exception):
    """Stands in for a chained exception whose own ``__str__`` published a live code."""


def _sanitised_cause(exc: BaseException, code: str, urls: Sequence[str]) -> BaseException:
    """The exception to chain: ``exc`` itself when it is safe, a stand-in when it cannot be.

    :func:`_redact_chain` works by rewriting ``args``, and that is enough for every
    exception whose ``str()`` is built from ``args`` — which is most of them, and all of the
    ones this package raises. It is NOT enough for a class that overrides ``__str__``:
    ``OSError`` does, and so does a good deal of third-party code, and a provider is allowed
    to delegate to a library this repo has never seen. Such a cause renders its own message
    from its own fields, ignores the rewritten ``args`` entirely, and prints in full through
    ``traceback.format_exception`` AND the C-level excepthook — neither of which this package
    can intercept, because both read the exception object rather than asking it anything.

    The only move left is not to chain that object. The stand-in keeps the type name, the
    traceback frames (so the "where" survives) and the fingerprint that joins to the
    ``code_created`` record; it drops the rendered message, which is the part that could not
    be made safe. Chaining nothing at all would be worse — the refusal would lose its "why"
    — and chaining the original is the thing that leaks.

    **The ORACLE is the renderer, and that is the pass-4 correction.** This decision used to
    be taken on ``str(exc)``, which is not what a traceback prints. Two ordinary shapes
    walked straight through it, measured on both ``traceback.format_exception`` and the
    C-level ``sys.__excepthook__`` while ``str(exc)``, ``denial_reason`` and the persisted
    ``policy_event`` were all clean:

    * a cause carrying ``exc.add_note(f"upstream permalink was …?discount={code}")`` — PEP-678
      notes are printed by the renderer and are not part of ``str``; and
    * an ``ExceptionGroup``, whose ``str`` is ``"… (2 sub-exceptions)"`` while the renderer
      prints every member's own message. ``.exceptions`` is not ``__cause__``, so the walk
      above did not reach it either.

    The lesson is not "notes and groups too". It is that an oracle which disagrees with the
    renderer it exists to protect will keep losing to whatever CPython renders next, so the
    oracle now IS the renderer — :func:`~.redaction.render_can_publish`, which calls it. And
    because the render covers the whole chain, its members and their notes, the ACTION is
    total by construction: one object is substituted and everything reachable from it goes
    with it, rather than each renderable attribute being rewritten in place and hoped
    complete.

    **Pass 6 changed which way the oracle fails**, not what it asks. It used to be
    ``spells_code(rendered_exception(exc), code)``, and that composition cannot tell "the
    render was clean" from "the render could not be obtained" — both arrive as a string that
    does not spell the code. :func:`~.redaction.render_can_publish` separates them and treats
    the second as unsafe. See its docstring for why "one reader failed" is not "no reader
    can print it".
    """
    _redact_chain(exc, code, urls)
    if not code:
        return exc
    if not render_can_publish(exc, code):
        return exc
    replacement: BaseException = RedactedCause(
        f"{safe_token(type(exc).__name__, code, label='cause-type')}: "
        f"<redacted-cause:{code_fingerprint(code)}> — this chained exception could not be "
        f"shown to be safe to render (its own __str__, a PEP-678 note or an ExceptionGroup "
        f"member spelled a recoverable discount code, or it could not be rendered at all), "
        f"and rewriting args cannot reach any of those, so the exception itself was "
        f"replaced (T-215)"
    )
    try:
        replacement = replacement.with_traceback(exc.__traceback__)
    except Exception:  # pragma: no cover
        pass
    return replacement


@dataclass(frozen=True, repr=False)
class OrphanedCode:
    """A discount code that EXISTS in the merchant's system for a checkout that was refused.

    Everything the exchange needs to record it and eventually revoke it, and nothing it has
    to go and look up: the code itself, where it would have sent the buyer, which provider
    issued it, and the three identifiers that place it in the ledger.

    **The default dataclass ``repr`` is suppressed, and that is load-bearing (T-215).** The
    whole point of this class is to carry a live discount past a refusal, and the generated
    ``repr`` would spell that discount — and the merchant's permalink — in the ordinary
    string form of *this* object, which reaches a log the moment anything formats it. The
    code is therefore reachable only through explicit field access — ``orphan.code``, which
    is exactly the deliberate act the revocation caller performs and the accidental act a
    log formatter does not.

    **What that does NOT buy, corrected in pass 6 because the docstring claimed it and the
    claim is measurably false.** It does not make ``f"{result}"`` safe for an
    :class:`~apps.exchange.src.accept.offer.AcceptResult` holding one. Measured: an
    ``AcceptResult`` from a refused off-domain checkout renders the live code through
    ``repr`` anyway, because :attr:`~apps.exchange.src.accept.offer.AcceptResult.events`
    carries the ``code_created`` event whose ``payload["code"]`` is the code in full — and on
    the *accepted* path :attr:`AcceptResult.code` is the code, as a plain top-level field.
    Neither is a defect: a result that hands the buyer a discount is *made of* that discount,
    and the ``code_created`` event is the sanctioned home this ticket deliberately puts it in.
    The surface T-215 protects is the persisted ``policy_event`` and the prose that reaches
    it, not the in-process result object, and the two must not be confused — believing the
    result object is scrubbed is how something starts logging it.
    """

    code: str
    permalink_url: str
    provider: str
    store_id: str
    auction_id: str
    bid_ref: str
    expires_at: float | None = None

    def __repr__(self) -> str:
        # Every field here except `provider` (a registry key this package owns) and
        # `expires_at` (a float) arrives from the merchant's reply or from the bid the
        # merchant wrote, so each is rendered through the same fail-closed guard the prose
        # sites use rather than trusted because it "is an identifier".
        code = self.code
        return (
            f"OrphanedCode(code=<{code_fingerprint(code)}>, "
            f"permalink_url={redact_url(self.permalink_url, code)!r}, "
            f"provider={self.provider!r}, "
            f"store_id={safe_token(self.store_id, code, label='store')!r}, "
            f"auction_id={safe_token(self.auction_id, code, label='auction')!r}, "
            f"bid_ref={safe_token(self.bid_ref, code, label='bid')!r}, "
            f"expires_at={self.expires_at!r})"
        )


class OrphanedCheckoutCode(Exception):
    """A refusal that happened **after** a real code was minted (T-157).

    The port's whole ordering discipline — resolve the domain, check the offer's URL, check
    the offer's fields, and only then mint — exists so that a refusal costs nothing. One
    check cannot be moved ahead of the mint, and this is it: the permalink a provider hands
    back does not exist until the provider has run, and for the Shopify adapter running means
    the merchant's ``POST /codes`` has already issued a live single-use discount.

    Refusing there is right. Losing the code is not. Before this class the refusal raised a
    bare :class:`~.domain.OffDomainCheckout` naming only the URL, so a real discount sat in
    the merchant's account with no ``code_created`` event anywhere in the exchange — nothing
    to revoke it by, nothing to expire it by, and nothing for reconciliation to notice.

    :attr:`orphan` is what the caller records and revokes. Catch this before
    ``OffDomainCheckout`` when you can do something with it; catching only the latter still
    works, which is why the domain-flavoured subclass below exists.

    **The message is redacted; the attribute is not (T-215).** The two are different
    audiences. A refusal message is prose, and prose from an exception ends up in logs and —
    via ``accept()`` — inside a persisted ``policy_event`` payload that the published API
    types as a bare string, readable by anything with the event stream. A live discount this
    layer cannot revoke must not be published there, so :meth:`__init__` runs the message
    through :func:`redact_code`. The real code stays on :attr:`orphan`, for the caller whose
    job is to record it. Redacting in the constructor rather than at each call site is the
    point: a *new* caller that formats this exception naively is safe by construction.

    **What "safe by construction" costs, and why the first attempt did not have it.** That
    attempt redacted by replacing three literal spellings of ``orphan.code``. But the
    message it redacts embeds the merchant's ``permalink_url``, and the code and the
    permalink are two independent merchant-authored fields that nothing requires to agree
    on spelling — so a lower-cased or percent-encoded permalink walked the live code
    through, into both the persisted event and the client-visible reason. The constructor
    now passes ``urls=(orphan.permalink_url,)``, and :func:`redact_url` reduces that URL —
    an exact value this exception already holds — to its scheme and host. The redaction no
    longer asks how the merchant spelled anything, so no encoding of the code *inside that
    URL* can defeat it.

    **And that is where the claim stops — pass 2 overstated it and pass 3 measured the
    difference.** This constructor is a *boundary* redactor: it can only remove what it was
    handed, which is the code and the URLs the orphan carries. It cannot remove a
    merchant-controlled value that some other module copied, normalised and formatted into
    the sentence before it got here — and ``assert_on_domain`` does exactly that with the
    permalink's host, in a lower-cased second copy that no URL value this exception holds
    contains. An ordinary uppercase code spelled in the host walked through this constructor
    untouched. The defence therefore lives at the sites that BUILD prose (see
    :mod:`.redaction`); these two layers are the backstop behind it, not the plan.

    **The chained exception is redacted too**, which the constructor alone cannot do: the
    cause is attached by ``raise … from`` only *after* ``__init__`` has returned, and the
    cause is the exception that named the offending permalink. That is handled in two
    places, deliberately, because one of them cannot reach every reader:

    * every ``raise … from`` site in this package calls :func:`_redact_chain` on the cause
      **before** chaining it, mutating ``args`` in place. This is what closes the
      interpreter's default excepthook, which reads the C-level cause slot through
      ``PyException_GetCause`` and runs no Python-level property at all; and
    * ``__cause__``/``__context__`` are properties here as well, redacting on the way out,
      so a future ``raise`` site that forgets the first half is still safe for every reader
      that goes through attribute lookup — :mod:`traceback`, ``logging(exc_info=True)``,
      pytest's reporter.
    """

    def __init__(self, message: str, *, orphan: OrphanedCode) -> None:
        # T-215: the code is redacted HERE, not at the call site that formats this exception.
        # A caller that logs `f"{exc}"` — `accept()` does exactly that, into a persisted
        # `policy_event` whose reason the published OpenAPI types as a bare string — must not
        # be able to publish a live discount by being naive, and neither must the next caller
        # that does the same. Because the redaction happens before `Exception.__init__`, the
        # code is absent from `args`, `str()`, `repr()`, the traceback line and anything
        # pickled from it. `orphan` still carries the real code for whoever can revoke it.
        #
        # `urls=` is what makes this structural rather than a spelling blacklist. The orphan
        # ALREADY holds the merchant's permalink, so the message's copy of it is replaced by
        # exact string match with a scheme-and-host reduction — no question is asked about
        # how that URL spells the code, which is the question the first attempt got wrong.
        super().__init__(redact_code(str(message), orphan.code, urls=(orphan.permalink_url,)))
        self.orphan = orphan

    # `raise X from Y` sets the cause through `PyException_SetCause`, which writes the C slot
    # and never calls a Python-level `__set__` — so the write cannot be intercepted. Every
    # READ can be: `traceback`, logging's `exc_info`, and `pytest`'s reporter all reach the
    # chain by ordinary attribute lookup, and this data descriptor on the subclass shadows
    # `BaseException`'s for that lookup. Redacting here rather than at the three `raise`
    # sites is deliberate for the same reason the constructor redacts: a NEW site that writes
    # `raise OrphanedCheckoutCode(...) from exc` is safe without knowing this exists.
    @property
    def _orphan_code(self) -> str:
        # `getattr`, not `self.orphan.code`: these properties are reachable before `__init__`
        # has finished (a traceback rendered while the exception is being built), and an
        # AttributeError raised from `__cause__` would turn a refusal into a crash — the exact
        # A5 outcome this whole path exists to avoid.
        orphan = getattr(self, "orphan", None)
        return getattr(orphan, "code", "") or ""

    @property
    def _orphan_urls(self) -> tuple[str, ...]:
        """The merchant URLs this exception holds as VALUES, for the structural redaction.

        ``str(...)`` is inside the guard because a ``permalink_url`` is whatever the provider
        put on :class:`MintedCheckout` — nothing coerces it to ``str`` on the way in — and
        this property is reached from ``__cause__``, i.e. from inside a traceback render. An
        unguarded conversion here turns "the merchant answered oddly" into "rendering the
        refusal raises", and a refusal nobody can render is a refusal nobody can act on.
        """
        orphan = getattr(self, "orphan", None)
        try:
            return (str(getattr(orphan, "permalink_url", "") or ""),)
        except Exception:
            return ()

    def _sanitised_slot(self, slot: Any) -> BaseException | None:
        """One chained-exception slot, made safe on the way out — and written back.

        Pass 3 ran only :func:`_redact_chain` here, which rewrites ``args`` and nothing
        else, so this lazy half was strictly weaker than the eager half at the ``raise``
        sites: a cause carrying a PEP-678 note or an ``ExceptionGroup`` member survived it.
        It now runs the same :func:`_sanitised_cause` the ``raise`` sites do, so both halves
        make the identical decision from the identical render-faithful oracle.

        **The write-back is the point, not a side effect.** Substituting only in the value
        returned would leave the C-level slot still pointing at the leaking object, and the
        C-level excepthook reads that slot directly — so a reader that went through
        attribute lookup would be safe while ``PyErr_Display`` published the code. Storing
        the stand-in repairs the slot for every subsequent reader, whichever door it uses.

        **Why the decision is remembered, and why it has to be re-entrant (pass 5).** Pass 4
        made this property RENDER, and rendering an exception READS its ``__cause__`` — so
        this method now calls, by way of :func:`~.redaction.rendered_exception`, the very
        renderer whose chain walk calls it back. Two distinct failures come out of that, and
        the one table below closes both:

        * **Non-termination.** ``a.__cause__ = b; b.__cause__ = a`` re-enters *this exact*
          decision — same object, same slot — and neither :mod:`traceback`'s own ``_seen``
          set nor a cycle check over the chain can see it, because the recursion is through
          a Python property rather than through the chain. On re-entry the raw slot value is
          returned: the decision one frame up is still running and is the one that will
          publish, and the renderer that asked is only LOOKING — handing it the real object
          is what lets the outer decision see the whole cycle instead of a hole in it.
        * **Combinatorial cost.** ``TracebackException.__init__`` reads ``__cause__`` twice
          per node (``... is not None``, then ``id(...) not in _seen``) and *then* recurses
          into it, so every level re-renders the whole suffix several times over. Measured
          on this tree, rendering a chain of plain orphan refusals with the decision not
          remembered: depth 6 took 0.24 s, depth 8 took 12.4 s, and depth 10 had not
          finished after 180 s — on the REFUSAL path, where the merchant chooses the depth.
          A refusal that hangs is a worse defect than the leak this exists to close. With
          one decision per ``(slot, object in it)`` pair the same walk is 0.0004 s at depth
          8 and 0.18 s at depth 160.

        The memo is keyed on the object the slot actually holds, so replacing the cause
        through the setter re-decides rather than returning a stale verdict.
        """
        current = slot.__get__(self)
        if current is None:
            return None
        decisions: dict[str, Any] = self.__dict__.setdefault(_SLOT_DECISIONS, {})
        name = str(getattr(slot, "__name__", slot))
        decided = decisions.get(name)
        if decided is _DECIDING:
            return current
        if decided is not None and decided[0] is current:
            return decided[1]  # type: ignore[no-any-return]
        decisions[name] = _DECIDING
        try:
            code = self._orphan_code
            if not code:
                _redact_chain(current, code, self._orphan_urls)
                safe = current
            else:
                safe = _sanitised_cause(current, code, self._orphan_urls)
                if safe is not current:
                    slot.__set__(self, safe)
        finally:
            decisions.pop(name, None)
        decisions[name] = (slot.__get__(self), safe)
        return safe

    @property
    def __cause__(self) -> BaseException | None:
        return self._sanitised_slot(_BASE_CAUSE)

    @__cause__.setter
    def __cause__(self, value: BaseException | None) -> None:
        _BASE_CAUSE.__set__(self, value)

    @property
    def __context__(self) -> BaseException | None:
        return self._sanitised_slot(_BASE_CONTEXT)

    @__context__.setter
    def __context__(self, value: BaseException | None) -> None:
        _BASE_CONTEXT.__set__(self, value)

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        # `BaseException.__reduce__` rebuilds an exception by calling its class with
        # `self.args`, which here is `(message,)` — and `orphan` is keyword-ONLY, so the
        # default would raise `TypeError` on the way back and turn a recoverable refusal
        # into a crash in whatever crossed the process boundary. The orphan is the whole
        # payload; it has to survive the round trip.
        return (_rebuild_orphaned, (type(self), self.args, self.orphan))


class OrphanedOffDomainCheckout(OrphanedCheckoutCode, OffDomainCheckout):
    """The post-mint host check refused the provider's own permalink, and a code is live.

    Both parents are load-bearing. It **is** an off-domain refusal, and every existing caller
    — ``accept()`` included — catches ``OffDomainCheckout``; that must keep working unchanged.
    It is **also** an orphaned code, and a caller that wants to revoke it needs to be able to
    say so without matching on a message string.
    """


class OrphanedUnusableDiscount(OrphanedCheckoutCode, DiscountDoesNotApply):
    """R3's validation refused the code the provider minted, and that code is live.

    Both parents carry weight, for the same reason :class:`OrphanedOffDomainCheckout` has
    two. It **is** a :class:`~.validity.DiscountDoesNotApply` — a caller watching for "this
    discount will not apply" keeps catching it — and it is **also** an orphan: the merchant
    already issued the discount, so ``accept()`` must file the ``code_created`` record that
    makes it revocable rather than deny and forget.

    This is deliberately the SAME shape the off-domain post-mint refusal already uses rather
    than a third one. A conflict found after the mint and a permalink refused after the mint
    leave identical wreckage — a live single-use discount in a merchant's account for a
    checkout that never happened — so they are recorded identically, and an operator reading
    ``code_created`` with ``orphaned: True`` does not have to know which wall it hit.
    """


@dataclass(frozen=True)
class CheckoutRequest:
    """Everything a provider is allowed to see about a won offer.

    ``store_domain`` is *supposed* to be the seller's **registered** domain — the one the
    platform holds in ``app.sellers``. On a bid it is read as ``bid["store_domain"]``, and
    that is where the sharp edge is: **a bid is a store's own reply**, so a store that writes
    ``store_domain: "attacker.tld"`` beside ``checkout_url: "https://attacker.tld/…"``
    supplies both halves of the comparison and the host check admits its own domain. The
    check then rejects only a store that contradicts *itself*, which no attacker does.

    :attr:`registered_domains` is the fix and the reason this field exists: give the request
    the platform's own lookup and the port compares against **that**, ignoring whatever the
    bid claimed. Leave it unset and the legacy behaviour stands — the bid's word is taken —
    which is what the frozen contract (``bid['store_domain']``) currently pins, so wiring the
    source is a one-line change at the call site that builds this request rather than a
    change to any provider.
    """

    auction_id: str
    bid_ref: str
    store_id: str
    store_domain: str
    offer: Mapping[str, Any]
    mode: str = "redirect"
    #: The merchant ``POST /codes`` client, when one is injected. The simulated provider
    #: does not use it; the Shopify adapter does. Kept on the request so registering a
    #: further provider adds no parameter to ``accept()``.
    code_creator: Any | None = None
    now: float = 0.0
    #: The platform's registered-domain lookup (:class:`RegisteredDomains`), when the caller
    #: has one. Present, it overrides :attr:`store_domain` entirely and a store it has never
    #: heard of mints nothing — fail closed. Absent, :attr:`store_domain` is used as-is.
    registered_domains: Any | None = None

    @property
    def checkout_url(self) -> str:
        return str(self.offer.get("checkout_url") or "")


@dataclass(frozen=True)
class MintedCheckout:
    """What a provider returns: a code, and where to send the buyer to redeem it."""

    code: str
    permalink_url: str
    expires_at: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckoutResult:
    """What the port returns. ``events`` is the C11 sequence, in order."""

    code: str
    permalink_url: str
    events: Sequence[Mapping[str, Any]]
    mode: str
    provider: str
    checkout_token: str
    expires_at: float | None = None
    #: Whether the host this checkout was checked against came from the **platform**.
    #:
    #: ``False`` means the caller wired no :attr:`CheckoutRequest.registered_domains`, so
    #: :func:`registered_domain_for` fell back to ``request.store_domain`` — a field the
    #: bidding store wrote. The domain guard then compared one store-authored value against
    #: another and passed, which it always will: a store that writes ``attacker.tld`` into
    #: both halves agrees with itself. The check is *decorative* in that case and this flag
    #: is what says so, because a decorative pass and a real one are otherwise identical
    #: from the outside — the permalink looks the same, the events look the same, and an
    #: operator reading the ledger has no way to tell that nothing was verified.
    #:
    #: It is a flag rather than a refusal for one reason, and it is worth stating plainly:
    #: with no external source there is *no information* that separates a legitimate bid
    #: from a spoofed one, so the port cannot decide. Only the caller can, by passing the
    #: platform's lookup. ``unbound_checkout_requests`` in :mod:`.lint` is what makes that
    #: non-optional at the call sites, and :mod:`.sellers` is what they pass.
    domain_verified: bool = False

    @property
    def kinds(self) -> list[str]:
        return [str(event["kind"]) for event in self.events]


class CheckoutProvider:
    """The port. Subclass and implement :meth:`mint`; never override :meth:`checkout`."""

    #: Human-readable provider name, recorded on the result for operators — never used to
    #: branch on, because nothing downstream may behave differently per provider (C11).
    name: ClassVar[str] = "checkout-provider"

    #: Whether this provider mints somewhere else and therefore needs an injected client.
    #:
    #: **Not a branch on the provider, and it must never become one.** C11 forbids anything
    #: downstream behaving differently per provider, and nothing here does: the port's
    #: sequence, its domain check and its orphan handling are identical whatever this says.
    #: What it lets a COMPOSITION ROOT ask — before a request exists — is whether the mode it
    #: is about to serve reaches a door outside this process. ``SimulatedRedirectProvider``
    #: mints locally and answers ``False``; :class:`~.providers.ShopifyCheckoutProvider`
    #: delegates to the merchant's ``POST /codes`` and answers ``True``.
    #:
    #: It exists because the alternative was a list of mode SPELLINGS in the composition root
    #: (``{"shopify", "shopify_stub"}``), which is a second registry that stops agreeing with
    #: this one the first time somebody calls :func:`~.registry.register_provider` — and the
    #: failure it produces is the silent one: an exchange composed with no creator, refusing
    #: every accept it serves with ``checkout_refused``. A provider that needs the client says
    #: so here, where it is registered, and the deployment is refused at wiring time instead.
    requires_code_creator: ClassVar[bool] = False

    #: Methods the port performs on every provider's behalf. Overriding one would let an
    #: implementation opt out of a guarantee the port makes, so it is refused.
    _FINAL_METHODS: ClassVar[frozenset[str]] = frozenset({"checkout", "_mint_recording_orphans"})

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        overridden = sorted(name for name in cls._FINAL_METHODS if name in cls.__dict__)
        if overridden:
            raise PortMethodIsFinal(
                f"{cls.__name__} overrides {overridden}, which CheckoutProvider performs for "
                f"every provider (the registered-domain check and the C11 event sequence). "
                f"Implement mint() instead."
            )

    # --- the final template method -------------------------------------------------
    def checkout(self, request: CheckoutRequest) -> CheckoutResult:
        """Turn a won offer into a code and a permalink, with the port's guarantees applied.

        Order matters and is asserted downstream: the domain check runs **before** ``mint``,
        so a refused offer has no code created for it anywhere — not by this provider, not
        by an injected merchant client, not by Shopify.
        """
        # 1. Resolve the trusted half FIRST. If the caller wired a registered-domain
        #    source, the bid's claim about its own domain is discarded here. If it did not,
        #    `registered` IS the bid's claim and everything below compares the store's word
        #    against the store's word — recorded, so the result and the ledger say so.
        verified = domain_is_platform_verified(request)
        registered = registered_domain_for(request)

        # 2. Untrusted input, checked against the registered domain, before anything else.
        #
        #    Only when there IS one. "No checkout_url" and "a checkout_url pointing at
        #    attacker.tld" are not the same condition and must not get the same answer:
        #    `collect_bids` manufactures a list-price fallback offer for every Tier-0 and
        #    silent store (R10) out of catalog data, not a store's reply. That offer used to
        #    reach this port with no checkout_url at all, so refusing an absent URL refused
        #    every fallback bid the exchange had just built for itself, and a Tier-0 store
        #    could be ranked and shortlisted but never bought from.
        #
        #    `ranking/candidates.py` now completes a FALLBACK entry's offer with a
        #    checkout_url built from the platform-registered domain it has already looked
        #    up. A doc sweep (edbc422) concluded from that this comparison had started
        #    running on a shortlisted fallback, and wrote so here. It was PREMATURE rather
        #    than wrong: the chain it described was real and broke one file over, and
        #    T-349 has since joined it up.
        #
        #    What used to break it: `auction/routes.py` handed `collected_bid_records` the
        #    value `ranking["candidates"]`, which is NOT the projection above —
        #    `ranking/__init__.py` sets `"candidates": rows`, the rank-ROW projection, with
        #    no `offer` and no `store_domain` — so the book recorded
        #    `candidate.get("offer") or {}` -> `{}` for a hosted bid and a fallback alike,
        #    and step 2 had nothing to look at for either. `rank_auction` now also returns
        #    the projection under `"projected"` and `merged_candidates` joins the two, so a
        #    served bid arrives here carrying its real `checkout_url` and this comparison
        #    runs BEFORE the mint. Measured over the HTTP door:
        #
        #        {"bid_id": "…:s1", "store_domain": "s1.example.com",
        #         "offer": {"checkout_url": "https://s1.example.com/cart/44352913:1", …}}
        #
        #    Absent is also reachable for reasons that have nothing to do with that: a direct
        #    caller of `checkout()` can hand over an offer that has none, and
        #    `NoRegisteredDomains` — the fail-closed wired default — holds no domain to build
        #    one from.
        #
        #    Nothing is relaxed by allowing it: with no URL there is no untrusted host in
        #    play at all, the provider builds the permalink from `registered` below, and
        #    step 5 validates that. A seller with no registered domain still cannot get a
        #    code — the permalink it would be built from has no host to match.
        if request.checkout_url:
            assert_on_domain(request.checkout_url, registered, what="offer checkout_url")

        # 3. The rest of the offer must be usable too, and this has to happen before the
        #    mint: the Shopify adapter's mint issues a real merchant discount, and a field
        #    that only blows up afterwards leaves that code live and unrecorded.
        deadline = assert_offer_is_mintable(request.offer, now=request.now)

        # 3b. R3's VALIDATE half, the part that needs nothing but the offer and the clock:
        #     the window the code would be minted INTO must still be open.
        #
        #     `deadline` is D22's arithmetic — `min(now + 48h, offer.expires_at)` — computed
        #     by the call above and HANDED BACK rather than recomputed, so the offer's
        #     `expires_at` is read exactly as many times as it was before this check
        #     existed. `ShiftingOffer` in `test_orphaned_code.py` is why that matters: a
        #     merchant's mapping may answer differently on a second read, and a gate that
        #     judged a different value from the one the code is minted out of is not a gate.
        #     So this line is a comparison and nothing else.
        #
        #     An offer that expired before `now` can only produce a code that is already
        #     dead, and until this line the port MINTED one anyway. Measured over the served
        #     route with an offer an hour past its expiry:
        #
        #         200 {"code": "PSX-6SMY54PJ",
        #              "permalink_url": "https://store-a.example.com/cart/1:1?discount=…"}
        #
        #     — a live single-use discount on a permalink handed to a shopper, whose window
        #     the port had computed and published without ever looking at it.
        #
        #     It sits HERE, ahead of the mint, on purpose: a closed window is a property of
        #     the offer alone, so discovering it after `POST /codes` has issued a real
        #     discount would manufacture an orphan out of a condition that was knowable for
        #     free. Nothing is minted, no code is stranded, and the buyer gets the ordinary
        #     `checkout_refused` denial with the next slot re-offered (A5).
        assert_the_window_is_open(deadline, request.now, what="the offer's own expiry")

        # 4. The provider's only job.
        #
        #    Everything from here on runs with a REAL code in existence — for the Shopify
        #    adapter, one the merchant's `POST /codes` has already issued — so every failure
        #    below is a refusal that leaves a live discount behind. It is carried out on the
        #    exception rather than dropped; see `OrphanedCheckoutCode`.
        #
        #    ...and INSIDE `mint` too, which is what the ledger is for. A code exists from
        #    the middle of `mint`, not from its return: `SimulatedRedirectProvider` (D45's
        #    required starting implementation) calls `mint_code` and then
        #    `default_permalink`, which resolves the registered domain a second time — and a
        #    lookup that answers once and fails once loses the code, because an exception
        #    out of `mint` has no channel to carry it. Only the Shopify adapter had closed
        #    that, with its own `except`; a guarantee one implementation remembered is not a
        #    guarantee, so it lives here where `__init_subclass__` makes it unavoidable.
        minted = self._mint_recording_orphans(request)
        orphan = OrphanedCode(
            code=minted.code,
            permalink_url=minted.permalink_url,
            provider=self.name,
            store_id=request.store_id,
            auction_id=request.auction_id,
            bid_ref=request.bid_ref,
            expires_at=minted.expires_at,
        )

        # 5. The provider's OWN output is untrusted too: a buggy or hostile adapter that
        #    returns a permalink on another host must not be able to hand the buyer over.
        #
        #    This is the one check that CANNOT be hoisted ahead of the mint — the permalink
        #    does not exist until the provider has run — and it is reachable on a perfectly
        #    legal offer. It used to be reachable by the most ordinary route there is: the
        #    R10 list-price fallback carried no `checkout_url`, so step 2 had nothing to look
        #    at and this was the first failable host comparison for an everyday bid. A
        #    shortlisted fallback now arrives with a URL — `ranking/candidates.py` completes
        #    it from the platform's registered-domain lookup, and
        #    `auction/routes.py::collected_bid_records` builds the bid book from those
        #    candidates — so step 2 checks it first. This check is unchanged and is still the
        #    only one that can fail with a live code behind it: passing step 2 says nothing
        #    about what the provider hands back, and an offer with no URL at all — a direct
        #    call, or a fallback for a store the platform holds no domain for — still reaches
        #    here having faced no host comparison.
        try:
            # `secret=` is T-215 pass 3, and it is the ONLY difference between this call and
            # the pre-mint one at step 2. From here on a real discount exists, so every
            # merchant-controlled fragment `assert_on_domain` formats — the host it parses,
            # the scheme, a `urlsplit` error's quoted text, the URL itself — is a fragment
            # that must not spell that discount. Passing the code is what lets the site that
            # BUILDS the prose make that call; two earlier fixes tried to make it downstream,
            # where the merchant-controlled fragments are no longer distinguishable from the
            # sentence around them, and both were defeated.
            assert_on_domain(
                minted.permalink_url,
                registered,
                what=f"{self.name} permalink_url",
                secret=minted.code,
            )
        except OffDomainCheckout as exc:
            # Redact the CAUSE before chaining it, not only through the reading properties
            # below. `raise … from exc` stores this object in the C-level cause slot, and
            # the interpreter's default excepthook reads that slot with
            # `PyException_GetCause` — no Python attribute lookup, so no property runs.
            # Rewriting `exc.args` here changes the object itself, which is the only edit
            # `PyErr_Display` can see. Measured leaking before this line existed.
            cause = _sanitised_cause(exc, minted.code, (minted.permalink_url, request.checkout_url))
            raise OrphanedOffDomainCheckout(
                # The fingerprint, not the code: this message is formatted into a persisted
                # `policy_event` by `accept()` (T-215). `{exc}` survives because the refused
                # HOST is the diagnostic, and `assert_on_domain` was given `secret=` above,
                # so every merchant-controlled fragment inside `exc` has already been
                # rendered safe at the point it was built rather than left for a downstream
                # replace to find.
                #
                # `store_id` is read straight off the bid (`accept()` does
                # `str(_read(bid, "store_id"))`), so it is merchant-controlled too: a store
                # is free to name itself after the code it is about to mint. `safe_token`
                # keeps it in the message in the ordinary case and drops it in that one.
                f"{exc} — the discount code {code_fingerprint(minted.code)} was ALREADY "
                f"minted by {self.name!r} for store "
                f"{safe_token(request.store_id, minted.code, label='store')!r} and is live "
                f"in the merchant's system; record and revoke it via the code_created event",
                orphan=orphan,
            ) from cause

        # 5b. R3's VALIDATE half again, now on what the provider ACTUALLY handed back — the
        #     one reading that can differ from step 3b's, and the only place the cart is
        #     visible at all.
        #
        #     Two questions, and the scope of the second is the whole of what this port may
        #     claim (see :mod:`.validity`):
        #
        #     * the window the PROVIDER published. `MintedCheckout.expires_at` is the
        #       provider's own answer and the one carried into the `code_created` body and
        #       onto `CheckoutResult`; a merchant that issued a code expiring before this
        #       checkout completes has said so here and nowhere else.
        #     * the cart. R3's last clause is "redirect … with the code pre-applied", and
        #       the Shopify adapter takes `reply["permalink_url"]` verbatim. A permalink
        #       that pre-applies a DIFFERENT code sends the buyer into a cart already
        #       carrying a discount this one does not combine with: they redeem somebody
        #       else's code, the single-use code the exchange recorded stays live and
        #       unredeemed, and `apps/trust`'s reconciler — which joins an order to an offer
        #       on `discount_codes[].code` — can never match the order to this acceptance.
        #
        #     Whether the SHOP's automatic discounts combine is not asked here and must not
        #     be: that answer needs the Admin API and lives in
        #     `apps/merchant/svc/src/codes/combines.py`, behind the merchant's own served
        #     `POST /codes`. This is the half the exchange can see for itself.
        #
        #     A failure here is post-mint, so it is an ORPHAN refusal in exactly the shape
        #     the off-domain permalink already uses — never a bare raise: `accept()` reads
        #     `OrphanedCheckoutCode.orphan` and files the `code_created` record that makes
        #     the live discount visible and revocable (T-202), and if that body will not
        #     validate, `_published_or_recorded` puts it down the audit-anomaly channel
        #     (T-283). Both are existing channels; a conflict does not get a third one.
        try:
            assert_the_window_is_open(
                minted.expires_at, request.now, what=f"{self.name} expires_at"
            )
            assert_the_cart_applies_this_code(
                minted.permalink_url, minted.code, what=f"{self.name} permalink_url"
            )
        except DiscountDoesNotApply as exc:
            # Sanitised before chaining, for the reason spelled out at the matching
            # off-domain site above: `raise … from` writes the C-level cause slot, which the
            # default excepthook reads without running any Python-level property.
            cause = _sanitised_cause(exc, minted.code, (minted.permalink_url, request.checkout_url))
            raise OrphanedUnusableDiscount(
                # `{exc}` is safe to interpolate BECAUSE `validity` guards its own
                # fragments: the rival discount code is a merchant value and goes through
                # `safe_token` at the site that builds the sentence, and our own code is
                # named only by its fingerprint. `store_id` is read straight off the bid, so
                # it is merchant-controlled here exactly as it is two handlers up.
                f"{exc} — the discount code {code_fingerprint(minted.code)} was ALREADY "
                f"minted by {self.name!r} for store "
                f"{safe_token(request.store_id, minted.code, label='store')!r} and is live "
                f"in the merchant's system; the buyer is not redirected, and the code must "
                f"be recorded and revoked via the code_created event",
                orphan=orphan,
            ) from cause

        try:
            checkout_token = secrets.token_hex(16)
            # Resolved ONCE, above, and handed to both the result and the events. It used to
            # be computed only for the result, which is half of why the `code_created` event
            # could not carry its published `expires_at`: there was no single value to put
            # in it, and a second `code_expiry(...)` call here would be a second answer to
            # the question "when does this discount die" for one discount (T-235).
            expires_at = (
                minted.expires_at
                if minted.expires_at is not None
                else code_expiry(request.now, request.offer)
            )
            return CheckoutResult(
                code=minted.code,
                permalink_url=minted.permalink_url,
                events=self._events(
                    request, minted, checkout_token, verified=verified, expires_at=expires_at
                ),
                mode=request.mode,
                provider=self.name,
                checkout_token=checkout_token,
                domain_verified=verified,
                expires_at=expires_at,
            )
        except Exception as exc:
            # Building the result cannot normally fail — step 3 already proved the offer's
            # fields parse, and `_events` reads nothing else. But "cannot normally fail" is
            # exactly the assumption that produced this ticket, and the cost of being wrong
            # is another unrevokable discount. A failure here is reported with the code
            # attached like any other post-mint refusal.
            cause = _sanitised_cause(exc, minted.code, (minted.permalink_url, request.checkout_url))
            raise OrphanedCheckoutCode(
                # `{exc}` here is an ARBITRARY exception's text — it may have been built out
                # of merchant input by code neither this module nor `domain` owns, so it is
                # the one fragment in this package that cannot be made safe at its build
                # site. It goes through `safe_token` whole: a message that could spell the
                # code is dropped rather than published, and the type name, the fingerprint
                # and the store still say what happened.
                #
                # The type NAME is merchant-controlled too, which is easy to miss because it
                # reads like metadata: a provider may delegate to a library, and a class
                # named after the code it is about to mint puts that code into every renderer
                # — `repr(exc)`, the traceback's final line, this sentence. It is a value a
                # merchant chose, so it is rendered like one.
                f"{safe_token(type(exc).__name__, minted.code, label='cause-type')}: "
                f"{safe_token(exc, minted.code, label='cause')} — raised AFTER "
                f"{self.name!r} minted {code_fingerprint(minted.code)} for store "
                f"{safe_token(request.store_id, minted.code, label='store')!r}; the code "
                f"is live and must be recorded and revoked",
                orphan=orphan,
            ) from cause

    # --- T-202, held by the PORT rather than by each provider -------------------------
    def _mint_recording_orphans(self, request: CheckoutRequest) -> MintedCheckout:
        """Call ``mint``, and turn a failure *after* a code exists into an orphan refusal.

        **The gap this closes.** ``checkout`` wraps everything after ``mint`` returns,
        because that is where T-157 lost a code. But the code comes into existence in the
        middle of ``mint``, and ``mint``'s only channel back is its return value — so an
        exception raised between the mint and the return carries nothing, and the port
        raises an ordinary refusal with ``orphaned_code`` ``None``. ``accept()`` then files
        no ``code_created`` event and the live discount is neither recorded nor revocable:
        the original T-157 defect, moved one frame deeper.

        Measured on ``SimulatedRedirectProvider`` — D45's *required* starting
        implementation, the one the whole starting-slice demo runs on — with a
        ``registered_domains`` lookup that answers the first call and raises
        ``RuntimeError('registry cache evicted')`` on the second, which is precisely the
        case ``providers.py`` documents ("a dropped connection, a cache eviction"). It mints
        with :func:`~.codes.mint_code`, then calls ``default_permalink``, which resolves the
        domain a second time. Before this method the run raised a bare ``OffDomainCheckout``
        and the code was gone.

        ``ShopifyCheckoutProvider`` had closed the same hole with an ``except`` block of its
        own. That is one implementation remembering, and D45 exists to invite more: a
        provider written next year gets this the way it gets the domain check — by being
        unable to opt out of it.

        **Three cases, deliberately different:**

        * the provider already carried the code out itself (an ``OrphanedCheckoutCode``, as
          the Shopify adapter raises) — re-raised untouched, because it knows more than this
          method does: the merchant's permalink, its own name for the failure;
        * nothing was minted — re-raised untouched. A merchant that is simply down created
          no code, and inventing an orphan for it would file a ``code_created`` event for a
          discount that does not exist. This is the ``ExplodingMerchant`` control, and its
          refusal shape must not move;
        * a code exists and the provider lost it — the refusal becomes an orphan refusal
          carrying that code. ``OrphanedOffDomainCheckout`` when the failure was an
          off-domain one, so every existing ``except OffDomainCheckout`` keeps catching it.
        """
        with minting_ledger() as recorded:
            try:
                return self.mint(request)
            except OrphanedCheckoutCode:
                raise
            except Exception as exc:
                code = recorded[-1] if recorded else ""
                if not code:
                    raise
                # Every merchant-controlled fragment is guarded at THIS site, which is the
                # site that builds the sentence: `{exc}` is arbitrary post-mint text (the
                # failing domain lookup quotes the bid's own `store_domain` claim), and
                # `store_id` is read off the bid.
                cause = _sanitised_cause(exc, code, (request.checkout_url,))
                kind: type[OrphanedCheckoutCode] = (
                    OrphanedOffDomainCheckout
                    if isinstance(exc, OffDomainCheckout)
                    else OrphanedCheckoutCode
                )
                raise kind(
                    # The type name is a merchant-controlled fragment for the same reason as
                    # at the matching site in `checkout`: a delegating provider's library
                    # chooses the class, and a class name renders everywhere a message does.
                    f"{safe_token(type(exc).__name__, code, label='cause-type')}: "
                    f"{safe_token(exc, code, label='cause')} — raised INSIDE "
                    f"{self.name!r}'s mint(), after {code_fingerprint(code)} had already "
                    f"been minted for store "
                    f"{safe_token(request.store_id, code, label='store')!r}; the code is "
                    f"live and must be recorded and revoked",
                    orphan=OrphanedCode(
                        code=code,
                        # The permalink is what `mint` had not finished building; there is
                        # none to report. The CODE is what has to be revoked.
                        permalink_url="",
                        provider=self.name,
                        store_id=request.store_id,
                        auction_id=request.auction_id,
                        bid_ref=request.bid_ref,
                    ),
                ) from cause

    # --- the extension point --------------------------------------------------------
    def mint(self, request: CheckoutRequest) -> MintedCheckout:
        """Turn an already-domain-checked offer into a code and an on-domain permalink."""
        raise NotImplementedError(f"{type(self).__name__} must implement mint(request)")

    # --- C11: built by the port, identical for every provider ------------------------
    def _events(
        self,
        request: CheckoutRequest,
        minted: MintedCheckout,
        checkout_token: str,
        *,
        verified: bool = False,
        expires_at: float | None = None,
    ) -> list[Mapping[str, Any]]:
        """The C11 trio, each body checked against the shape ``contracts`` publishes for it.

        ``expires_at`` is the checkout's single resolved expiry, passed in rather than
        recomputed, so the ``code_created`` record and :attr:`CheckoutResult.expires_at`
        cannot disagree about when the discount dies.

        Built through :func:`build_published_event` rather than :func:`build_event`: this is
        the producing boundary ``contracts/src/ledger.py`` names, and the defect it closes is
        that ONE kind had TWO bodies — the orphan path (``accept()._orphan_record``) wrote
        the published ``code_created`` body while this success path wrote
        ``{checkout_token, discount_code}``, carrying none of ``code``, ``permalink_url`` or
        ``expires_at``. So anything that needed to revoke or expire a *live* discount found
        the fields present only on the refused one (T-235).

        **A body that will not validate costs this checkout its EVENT, never its code
        (T-283).** This method runs inside ``checkout``'s post-mint ``try``, whose handler
        opens with "building the result cannot normally fail" and turns anything reaching it
        into an :class:`OrphanedCheckoutCode` — so a raise from here refused a buyer a
        discount the merchant had already issued, over a bookkeeping mismatch. Measured, with
        ``contracts`` publishing one key this trio does not write: a live
        ``code:1ef0d265c7e7`` became an ``OrphanedCheckoutCode`` and the shopper got nothing.

        The check itself is untouched and still refuses the body — that is what keeps one kind
        from having two shapes — but the refusal is now *recorded* rather than *raised*:
        :func:`~..auction.ledger.record_audit_anomaly` keeps the missing record's kind, its
        published shape, the keys that were written and the join keys an operator needs, and
        the trio comes back one event short. Deliberately short rather than emitted through
        the unvalidated :func:`build_event`: a malformed body reaching the trust service under
        a published kind is precisely the defect T-235 closed, and "the record is missing" is
        a state a reconciler can act on while "the record is present and lying" is not. The
        surviving events carry ``checkout_token`` and the anomaly names it, so *which*
        checkout lost its record is still answerable.
        """
        offer = dict(request.offer)
        common = {"auction_id": request.auction_id, "store_id": request.store_id}
        bodies: tuple[tuple[str, dict[str, Any]], ...] = (
            (
                ACCEPTED_KIND,
                {
                    "checkout_token": checkout_token,
                    "bid_ref": request.bid_ref,
                    "offer": {
                        "product_ref": offer.get("product_ref"),
                        "unit_price": offer.get("unit_price"),
                        "total_price": offer.get("total_price"),
                        "discount": offer.get("discount"),
                        # The dispatch promise, carried for the same reason the price and the
                        # discount are: `trust.reconcile` grades a promise against what the
                        # webhooks observed, and it reads every one of them off THIS event
                        # (`engine._promised`). Left out, `promised_delivery_days` was always
                        # `None` on the minting path, so `delivery_comparable` was always
                        # False and `shipped_on_time` — one of the six trust dimensions —
                        # could never move for any store. `Offer.delivery_estimate_days` is a
                        # published, optional protocol field; the handoff path's `accepted`
                        # body (`accept._handoff_events`) has always carried the whole offer,
                        # so this makes the two accept paths gradeable alike rather than
                        # widening what the ledger is allowed to hold.
                        "delivery_estimate_days": offer.get("delivery_estimate_days"),
                    },
                },
            ),
            (
                CODE_CREATED_KIND,
                {
                    # The published `code_created` body (D24) — the same three keys the
                    # orphan record has always carried, so one kind is one shape whether the
                    # checkout completed or was refused after the mint.
                    "code": minted.code,
                    "permalink_url": minted.permalink_url,
                    "expires_at": expires_at,
                    # Kept beside them, not instead of them: `checkout_token` is the D24 join
                    # to `accepted`/`checkout_redirect`, and `discount_code` is the spelling
                    # `services/sim/src/runner.py` already reads (it accepts either), so
                    # adding the published keys breaks no existing reader.
                    "checkout_token": checkout_token,
                    "discount_code": minted.code,
                },
            ),
            (
                CHECKOUT_REDIRECT_KIND,
                {
                    "checkout_token": checkout_token,
                    "permalink_url": minted.permalink_url,
                    "discount_code": minted.code,
                    # The one thing an auditor cannot reconstruct from the rest of this
                    # event: whether the host the buyer is being sent to was checked
                    # against the platform's registry or against the seller's own word.
                    "domain_verified": verified,
                },
            ),
        )

        events: list[Mapping[str, Any]] = []
        for kind, payload in bodies:
            try:
                events.append(build_published_event(kind, payload=payload, **common))
            except MalformedLedgerPayload as exc:
                record_audit_anomaly(
                    # A code-authored call site, never `self.name`: a provider is free to
                    # name itself after the code it is about to mint (T-215), and the
                    # provider's own name goes in the context below where it is one value
                    # among several rather than the label the anomaly is filed under.
                    "CheckoutProvider._events",
                    kind,
                    exc,
                    payload=payload,
                    provider=self.name,
                    auction_id=request.auction_id,
                    store_id=request.store_id,
                    bid_ref=request.bid_ref,
                    # The join, and the only two values here that touch the discount: the
                    # token the surviving events carry, and the FINGERPRINT — never the code.
                    checkout_token=checkout_token,
                    fingerprint=code_fingerprint(minted.code),
                )
        return events


def _usable(domain: Any, request: CheckoutRequest) -> str:
    """A registered domain a permalink can actually be built on, or an explicit refusal."""
    if not domain or not str(domain).strip():
        raise OffDomainCheckout(
            f"no registered domain is on file for {request.store_id!r}, so there is no host "
            f"a checkout for it could be on (C10/D22)"
        )
    # A lookup that ANSWERS with an object rather than a domain used to have `str(<object>)`
    # — its address — carried onward as a "registered domain" and re-rendered with `!r` in
    # the off-domain message `domain.py` builds. BOTH branches are covered, and the `str`
    # one is not hypothetical: a registry whose `domain_for` returns `str(self._backend)`
    # hands back an ordinary `str` that an `isinstance` check waves through, and the address
    # then reached the 200 body as `https://<object object at 0x…>/cart/1:1` AND the
    # persisted `checkout_redirect` event. Measured on this branch by an adversarial pass,
    # and measured leaking at the fork point too — a hole the first version of this line
    # narrowed rather than closed.
    return redact_addresses(domain) if isinstance(domain, str) else describe(domain)


def domain_is_platform_verified(request: CheckoutRequest) -> bool:
    """True when the domain this checkout is checked against comes from the **platform**.

    False means :func:`registered_domain_for` will fall back to ``request.store_domain``,
    which the bidding store wrote — so the host check is comparing the store's claim to the
    store's claim and cannot fail for a store that is consistent about its own lie. See
    :attr:`CheckoutResult.domain_verified` for why that is recorded rather than refused, and
    ``lint.unbound_checkout_requests`` for what stops a call site from getting here.
    """
    return request.registered_domains is not None


def registered_domain_for(request: CheckoutRequest) -> str:
    """The domain the port compares against: the platform's, when the caller wired one.

    With no :attr:`CheckoutRequest.registered_domains` source this returns
    ``request.store_domain`` — the legacy behaviour the frozen contract pins, in which the
    bid supplies the domain it is checked against.

    With a source, the source wins outright and there is no falling back to the bid's claim:
    a lookup that raises, or that does not know the store, refuses the checkout. That is the
    only order that is safe — falling back on a lookup failure would mean a store could get
    its own claim honoured by making the lookup fail.

    Either way the result is a **usable** domain or an exception. It is called before
    ``mint``, so "there is no domain here" is settled while refusing still costs nothing;
    discovering it afterwards, when the permalink is checked, would mean the merchant had
    already issued a real code for a seller the platform cannot place.
    """
    source = request.registered_domains
    if source is None:
        return _usable(request.store_domain, request)

    lookup = getattr(source, "domain_for", None)
    if not callable(lookup):
        if not callable(source):
            # The TYPE, never `{source!r}` (T-264). This message is formatted straight into
            # `AcceptResult.denial_reason`, which is persisted into a `policy_event` and
            # returned to an unauthenticated caller — and the value that reaches here is
            # routinely an object with the default `__repr__`, so the repr rendered a literal
            # `<object object at 0x104e0a170>` into that payload. That is a process
            # memory-layout leak, and it also made one refusal render differently on every
            # run, so nothing downstream could group two of them. The type name is the part
            # an operator can act on.
            raise TypeError(
                f"registered_domains of type {type(source).__name__!r} exposes neither "
                f"domain_for(store_id) nor __call__(store_id)"
            )
        lookup = source

    try:
        domain = lookup(request.store_id)
    except Exception as exc:
        raise OffDomainCheckout(
            # T-264's own recorded reproduction lands HERE, not on the callability check
            # above: a `registered_domains` value that is callable and RAISES ON USE
            # passes that check, and its exception message — or its exception ARGUMENT,
            # for `KeyError(<object>)` — carries the address the whole way to the 409.
            f"registered domain lookup for {request.store_id!r} failed "
            f"({describe_exception(exc)}); refusing to check out against the bid's own "
            f"claim {request.store_domain!r}"
        ) from exc

    if not domain or not str(domain).strip():
        raise OffDomainCheckout(
            f"the platform holds no registered domain for {request.store_id!r}; the bid's "
            f"claim {request.store_domain!r} is not evidence of one"
        )
    return _usable(domain, request)


def default_permalink(request: CheckoutRequest, code: str) -> str:
    """The D22 cart permalink on the seller's registered domain, for providers that build one.

    **``or 1`` is gone, and it was the reading the protocol forbids in words.** This function
    read ``offer.get("variant_ref") or offer.get("variant_id") or 1``, while
    ``contracts.protocol.Offer.variant_ref``'s own docstring says: *"a fallback offer minted
    from a roster row names no variant at all. Absent means 'the bid did not name one', never
    'the default variant'."* ``1`` is not a neutral value on this field — it is a different,
    specific variant.

    What it produced, measured. Over ``fixtures/real-catalogs-demo`` (28,134 variant records,
    nineteen stores) the storefronts' own variant ids run 9 to 14 digits — histogram
    ``{9: 12, 10: 3, 11: 23, 12: 1, 13: 73, 14: 28022}`` — so ``1`` names no variant any of
    those stores issues. ``services/shopify-stub`` passes it through its ``isdigit()`` gate and
    then answers ``404 {"errors": "Variant 1 is not available"}`` — *after* a real single-use
    discount has been minted and recorded against it.

    **What changed, and what did not.** The offer now CARRIES a variant on the paths that
    could not name one before — ``retrieval.roster`` reads the storefront's own id off the
    same observed offer the price came from, ``RosterEntry`` declares it so a stated row is no
    longer silently discarded, and ``auction.collect._list_price_bid`` puts it on the fallback
    offer — so this function finds a real variant where it previously found nothing. The
    hosted path already did: ``deploy/demo/store-contexts/*.json`` carry 493 of 493 catalog
    rows with an all-digit ``variant_ref``.

    **What `1` still costs, and why it is still here.** It is a guess, and the honest
    behaviour is to decline. That refusal is NOT shipped, and the reason is measured rather
    than judged: it turns 62 tests in twelve files red, and among them are the frozen
    acceptance goals — ``.swarm-loop/acceptance/test_e3_exchange.py`` and
    ``test_spec_criteria.py`` both build offers spelled
    ``checkout_url = f"https://{domain}/cart/1:1?discount=NET"`` with no ``variant_ref``
    anywhere, so every redirect-mode mint in the frozen suite would decline. Frozen tests may
    not be edited. Closing this is a change to the FIXTURE CORPUS first and to this function
    second, and it is written down here rather than left as a silent default.

    Its blast radius is now much smaller than it was: the ORGANIC majority no longer reaches
    this function with an empty variant, and the pre-accept and post-accept fallback
    destinations (:func:`~apps.exchange.src.ranking.candidates.fallback_checkout_url`) no
    longer name variant ``1`` at all.

    ``variant_id`` is still read after ``variant_ref``, unchanged: it is the second spelling
    hosted agents use (``store_agent.runtime.bidding.VARIANT_REF_KEYS``), and both name the
    storefront's own id.

    **Quantity keeps its default and that is not an inconsistency.**
    :func:`~.codes.offer_quantity` answers ``1`` for an absent quantity because *buy one* is
    the semantic default for a quantity. There is no semantic default for a variant. The two
    reads sit on adjacent lines and must not get the same rule.
    """
    offer = request.offer if isinstance(request.offer, Mapping) else {}
    named = offer.get("variant_ref") or offer.get("variant_id")
    variant = str(named).strip() if named is not None and not isinstance(named, bool) else ""
    if not variant:
        # STILL A GUESS, AND STILL WRONG. See "What `1` still costs" above. It is left here
        # because removing it is not a change to this function: measured, a refusal turns 62
        # tests in twelve files red, and among them are the FROZEN acceptance goals, whose own
        # fixtures build every offer without a variant
        # (`.swarm-loop/acceptance/test_e3_exchange.py` and `test_spec_criteria.py` both spell
        # `checkout_url = f"https://{domain}/cart/1:1?discount=NET"` and name no variant at
        # all). Those may not be edited. Closing it is a fixture-corpus change first.
        variant = "1"
    return build_cart_permalink(
        shop_domain=registered_domain_for(request),
        code=code,
        variant_id=variant,
        quantity=offer_quantity(offer),
    )
