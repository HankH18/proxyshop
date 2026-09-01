"""Stand-in for the exchange, importing a sealed surface ON PURPOSE. D35 fixture.

C3/S7 says the exchange must have no code path that can read envelopes, and T-000 wired
``lint-imports`` into ``make verify`` to enforce it. A gate nobody has ever seen fail is a
gate nobody has evidence about: if the contract were mis-typed, if the root package list
missed ``exchange``, or if a future edit made the contract vacuous, ``make verify`` would
stay green and S7 would be only half-built. D35 therefore splits the work -- T-000 ships the
checker, T-011 ships the proof that it fires.

This package is the proof's negative control. It lives OUTSIDE every root package named in
the repo's ``.importlinter`` (``trust`` is ``apps/trust/src``, and this is
``apps/trust/tests/``), so the real gate never sees it and stays green. The test runs
``lint-imports`` against ``importlinter_fixture.ini`` beside this file, which names these
two stand-ins as its root packages, and asserts a NON-ZERO exit.
"""

from d35_sealed_fixture import envelope  # noqa: F401 - the violation is the point

VIOLATION = envelope.SEALED_ENVELOPE_MARKER
