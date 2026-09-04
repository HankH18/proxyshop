"""The one home for machinery several ``trust`` feature packages need (T-167).

Not a feature. There is no ``routes.py`` here on purpose, so ``trust.main``'s
``glob("*/routes.py")`` discovery never mounts anything from this directory, and the leading
underscore says the same thing to a reader.

Its only tenant today is :mod:`._binding`, the elected-primary module-binding shim. That
file used to exist as four byte-identical copies — one in each of ``scoring``, ``reconcile``,
``feedback`` and ``snapshot`` — because the shim has to be importable from inside each
package's ``__init__.py`` and no ticket owned a shared home for it. Nothing enforced that the
four stayed identical, so a fix applied to one and not the others would silently reintroduce
the dual-spelling module-identity bug the shim exists to prevent — a bug this repo already
measured 5 runs out of 5 before fixing it for ``trust.ledger``.

The four packages now reach it as ``from .._shared._binding import bind_submodules``. A
RELATIVE import is what makes one home possible at all: the package is reachable as both
``trust._shared`` and ``apps.trust.src._shared``, and a relative import resolves to whichever
spelling is executing, so neither spelling is hard-coded anywhere.

Import ORDER is unchanged and still matters. The *elected primary* block stays inlined at the
top of each feature ``__init__.py``, above this import, because it has to run before the
first relative import — and this import IS a relative import. Moving the shim did not move
that block and must not.
"""

from __future__ import annotations

__all__: list[str] = []
