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

from ..auction.ledger import build_event
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
    rendered_exception,
    safe_token,
    spells_code,
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


#: C11: the ordered `LedgerEvent` kinds every checkout emits, whichever provider ran.
CHECKOUT_EVENT_KINDS: tuple[str, str, str] = ("accepted", "code_created", "checkout_redirect")


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

        redacted = tuple(_redact_arg(arg, code, urls) for arg in node.args)
        if redacted != node.args:
            node.args = redacted

        # PEP-678 notes: a separate list, printed by the renderer, absent from `str()`.
        notes = getattr(node, "__notes__", None)
        if isinstance(notes, list):
            for index, note in enumerate(notes):
                cleaned = _redact_arg(note, code, urls)
                if cleaned != note:
                    notes[index] = cleaned

        # `ExceptionGroup.exceptions` — members the renderer prints and the chain omits.
        members = getattr(node, "exceptions", None)
        if isinstance(members, tuple):
            frontier.extend(m for m in members if isinstance(m, BaseException))

        for nxt in (_BASE_CAUSE.__get__(node), _BASE_CONTEXT.__get__(node)):
            if nxt is not None:
                frontier.append(nxt)


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
    if code and spells_code(str(arg), code):
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
    oracle now IS the renderer — :func:`~.redaction.rendered_exception`. And because the
    render covers the whole chain, its members and their notes, the ACTION is total by
    construction: one object is substituted and everything reachable from it goes with it,
    rather than each renderable attribute being rewritten in place and hoped complete.
    """
    _redact_chain(exc, code, urls)
    if not code:
        return exc
    if not spells_code(rendered_exception(exc), code):
        return exc
    replacement: BaseException = RedactedCause(
        f"{type(exc).__name__}: <redacted-cause:{code_fingerprint(code)}> — this chained "
        f"exception RENDERED a recoverable discount code (through its own __str__, a PEP-678 "
        f"note, or an ExceptionGroup member), which rewriting args cannot reach, so the "
        f"exception itself was replaced (T-215)"
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
    string form of the object. It reaches a log the moment anything does ``f"{result}"`` on
    an :class:`~apps.exchange.src.accept.offer.AcceptResult`, whose own generated ``repr``
    renders this one as a nested field; suppressing it here is what makes that nesting safe
    without a second ``repr`` on every dataclass that holds one. The code is therefore
    reachable only through explicit field access — ``orphan.code``, which is exactly the
    deliberate act the revocation caller performs and the accidental act a log formatter
    does not.
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
        """The merchant URLs this exception holds as VALUES, for the structural redaction."""
        orphan = getattr(self, "orphan", None)
        return (str(getattr(orphan, "permalink_url", "") or ""),)

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
        #    silent store (R10), and that offer carries no checkout_url by construction —
        #    it is catalog data, not a store's reply. Refusing an absent URL therefore
        #    refused every fallback bid the exchange had just built for itself, so a Tier-0
        #    store could be ranked and shortlisted but never bought from.
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
        assert_offer_is_mintable(request.offer)

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
        #    legal offer: the R10 list-price fallback carries no `checkout_url`, so step 2
        #    has nothing to look at and this is the first failable host comparison.
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

        try:
            checkout_token = secrets.token_hex(16)
            return CheckoutResult(
                code=minted.code,
                permalink_url=minted.permalink_url,
                events=self._events(request, minted, checkout_token, verified=verified),
                mode=request.mode,
                provider=self.name,
                checkout_token=checkout_token,
                domain_verified=verified,
                expires_at=(
                    minted.expires_at
                    if minted.expires_at is not None
                    else code_expiry(request.now, request.offer)
                ),
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
                f"{type(exc).__name__}: "
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
                    f"{type(exc).__name__}: "
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
    ) -> list[Mapping[str, Any]]:
        offer = dict(request.offer)
        common = {"auction_id": request.auction_id, "store_id": request.store_id}
        return [
            build_event(
                "accepted",
                payload={
                    "checkout_token": checkout_token,
                    "bid_ref": request.bid_ref,
                    "offer": {
                        "product_ref": offer.get("product_ref"),
                        "unit_price": offer.get("unit_price"),
                        "total_price": offer.get("total_price"),
                        "discount": offer.get("discount"),
                    },
                },
                **common,
            ),
            build_event(
                "code_created",
                payload={"checkout_token": checkout_token, "discount_code": minted.code},
                **common,
            ),
            build_event(
                "checkout_redirect",
                payload={
                    "checkout_token": checkout_token,
                    "permalink_url": minted.permalink_url,
                    "discount_code": minted.code,
                    # The one thing an auditor cannot reconstruct from the rest of this
                    # event: whether the host the buyer is being sent to was checked
                    # against the platform's registry or against the seller's own word.
                    "domain_verified": verified,
                },
                **common,
            ),
        ]


def _usable(domain: Any, request: CheckoutRequest) -> str:
    """A registered domain a permalink can actually be built on, or an explicit refusal."""
    if not domain or not str(domain).strip():
        raise OffDomainCheckout(
            f"no registered domain is on file for {request.store_id!r}, so there is no host "
            f"a checkout for it could be on (C10/D22)"
        )
    return str(domain)


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
            raise TypeError(
                f"registered_domains {source!r} exposes neither domain_for(store_id) "
                f"nor __call__(store_id)"
            )
        lookup = source

    try:
        domain = lookup(request.store_id)
    except Exception as exc:
        raise OffDomainCheckout(
            f"registered domain lookup for {request.store_id!r} failed "
            f"({type(exc).__name__}: {exc}); refusing to check out against the bid's own "
            f"claim {request.store_domain!r}"
        ) from exc

    if not domain or not str(domain).strip():
        raise OffDomainCheckout(
            f"the platform holds no registered domain for {request.store_id!r}; the bid's "
            f"claim {request.store_domain!r} is not evidence of one"
        )
    return _usable(domain, request)


def default_permalink(request: CheckoutRequest, code: str) -> str:
    """The D22 cart permalink on the seller's registered domain, for providers that build one."""
    offer = request.offer
    return build_cart_permalink(
        shop_domain=registered_domain_for(request),
        code=code,
        variant_id=offer.get("variant_ref") or offer.get("variant_id") or 1,
        quantity=offer_quantity(offer),
    )
