"""The ``exchange`` package root — and the one utility every subpackage of it needs.

Rendering arbitrary values into text that is safe to persist and to publish.

The defect this code exists to close (T-264, T-326, T-293, and the property T-327 states) is
one line long wherever it appears::

    raise CheckoutCreatorError(f"injected code creator {creator!r} exposes neither ...")

``creator`` is an object a *caller* injected. When it has CPython's default ``__repr__`` that
f-string renders ``<object object at 0x1045fd2e0>`` — this process's memory layout — and the
exchange then writes that string into a durable ``policy_event`` and returns it in the 409
body of ``POST /auctions/{auction_id}/accept``, which is unauthenticated. Two harms, not one:
an address leaks to a client, and the denial reason becomes unparseable and unstable, because
the same refusal renders differently on every run so nothing downstream can group two of them.

Why it lives in the PACKAGE ROOT, which was empty before
--------------------------------------------------------

The leaking f-strings are spread across ``checkout/``, ``eligibility/`` and ``accept/``, and
``accept.reasons`` already imports from ``eligibility`` — so a helper living in ``accept``
could not be imported by ``eligibility`` without a cycle, and the subpackage that leaks
hardest would have gone unfixed. The obvious answer, a sibling module
``apps/exchange/src/safe_text.py``, was written first and is **measurably wrong**: three
images ship parts of this package and only ``apps/exchange/Dockerfile`` copies
``apps/exchange/src/`` wholesale. ``apps/merchant/Dockerfile`` and ``services/sim/Dockerfile``
copy ``apps/exchange/src/__init__.py`` plus a named list of SUBDIRECTORIES, so a new top-level
module is not in either artifact — and
``proxyshop_support/tests/test_artifact_copyset.py::test_t301_every_shipped_module_imports_inside_the_container_shaped_tree``
said so out loud: 17 of 80 shipped merchant modules and 18 of 97 shipped sim modules stopped
importing, which in a real deployment is the merchant image failing at start-up.

``__init__.py`` IS in all three COPY sets, and every submodule import initialises it anyway,
so this is the only place in the package a cross-cutting leaf can live without either a new
import-direction inversion (``eligibility`` -> ``checkout``) or an edit to two Dockerfiles
that belong to other owners. Nothing here may import from ``exchange``: it must stay a leaf,
or the cycle it was moved to avoid comes back through the package root.

Two layers, deliberately
------------------------

:func:`describe` and :func:`describe_exception` are for the *sites that build prose* — an
operator wants ``<StaticSellerEligibility>``, not a deleted message, and a redactor at the
boundary cannot restore a type name that the site never wrote. :func:`redact_addresses` is the
*invariant*, applied where a reason becomes a published denial (``accept.reasons``,
``accept.gate``'s bypass branch, ``accept.routes._denied``). The second is not a substitute
for the first: it is what makes the property hold for an f-string nobody has written yet.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "ADDRESS",
    "describe",
    "describe_exception",
    "redact_addresses",
]

#: A process address as anything downstream would recognise it — and as the accept-path gate
#: ``apps/exchange/tests/test_accept_denials.py`` looks for it. Six hex digits rather than
#: two so an ordinary ``0x1f`` in prose is not mangled.
ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")

#: CPython's default ``__repr__``: ``<object object at 0x…>``, ``<Foo at 0x…>``, and the
#: dotted ``<module.Foo object at 0x…>`` a nested class produces. The class name is kept —
#: it is the half an operator can act on — and only the address is dropped.
_DEFAULT_REPR = re.compile(r"<([A-Za-z_][\w.]*)(?: object)? at 0x[0-9a-fA-F]+>")

#: Values whose ``repr`` is information rather than an address. Everything else is described
#: by its type — see :func:`describe`.
_REPR_IS_SAFE: tuple[type, ...] = (str, bytes, bool, int, float, complex, type(None))


def redact_addresses(text: Any) -> str:
    """``text`` with every process address removed, and every type name kept.

    Applied at the boundary where a denial reason is built or republished, this is what makes
    "no reason ever renders an object's default ``repr``" a property of the package rather
    than a property of the particular f-strings someone has audited. It is intentionally
    total: the collaborator's *own* exception message is redacted too, because a registry
    raising ``TypeError(f"the backend {object()!r} is unreachable")` leaks an address the
    exchange never quoted and republishes verbatim.
    """
    collapsed = _DEFAULT_REPR.sub(lambda match: f"<{match.group(1)}>", str(text))
    return ADDRESS.sub("0x<redacted>", collapsed)


def describe(value: Any) -> str:
    """A rendering of ``value`` that is safe to persist and to publish (T-264).

    Values whose ``repr`` carries meaning keep it; anything else is named by its type, which
    is the part an operator actually needs ("you passed a ``StaticSellerEligibility`` where a
    version string belongs"). A scalar's ``repr`` is still swept for an address, because a
    ``str`` whose *content* is an address is a value a caller can post.
    """
    if isinstance(value, _REPR_IS_SAFE):
        return redact_addresses(repr(value))
    return f"<{type(value).__name__}>"


def describe_exception(exc: BaseException) -> str:
    """``"TypeError: …"`` with the message's addresses removed and its class name kept.

    ``str(exc)`` is not safe on its own for two independent reasons, and the second is the one
    that is easy to miss: the message may *quote* an object, and ``str(KeyError(obj))`` simply
    **is** ``repr(obj)`` — a registry that does not know a store raises exactly that shape, so
    nothing has to quote anything for the address to travel.
    """
    return f"{type(exc).__name__}: {redact_addresses(exc)}"
