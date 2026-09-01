"""Stand-in for a sealed-state package. Test fixture for D35; not product code.

Nothing imports this from the real source tree. It exists only so that
``apps/trust/tests/lint_fixtures/importlinter_fixture.ini`` has a forbidden module to point
its contract at, and so the violation the D35 proof asserts on is a real import that
import-linter really finds -- not a hand-written expectation about what it would find.
"""
