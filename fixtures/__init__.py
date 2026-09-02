"""ProxyShop fixtures — the human-approved ground truth everything else is graded against.

`fixtures` is the one workspace member with no ``src/`` directory (T-000 / R1b): its data
directories are addressed directly by ticket scope globs and it is imported as a top-level
package from the repo root, which is what makes ``python -m fixtures.seed`` work.

What lives here, and why the direction matters
----------------------------------------------
``fixtures/manifest.json`` and ``fixtures/golden/golden_set.json`` are **ground truth**, not
outputs. SPEC A3 is the reason: "the trust engine catches the dishonest store" is a circular
claim if the dishonest behaviours are defined by the trust engine's own config. So the
behaviours, the episode budget, the new-store prior N, the expected trust trajectory, the
``claim_type -> dimension`` routing table and the golden pitch labels all live in one
document a **human approves**, and every consumer reads it rather than supplying its own
side of the comparison.

The importable surface:

* :mod:`fixtures.manifest` — load the manifest, with the D53 guard that an unmapped
  ``claim_type`` **raises** rather than silently defaulting to a dimension.
* :mod:`fixtures.generator` — the parameterised ``SEED_CATEGORY`` catalog generator:
  ``generate(seed_category, seed)`` (pure, deterministic) and ``apply(payload, target)``
  (idempotent upsert, mirroring the shopify-stub's ``POST /_stub/seed`` semantics).
* :mod:`fixtures.seed` — the ``make demo-seed`` entry point that drives a real
  shopify-stub over HTTP with the generator's payload.
* :mod:`fixtures.approval` — the tool a **human** runs to record their approval of the
  manifest. No agent may run it (EXECUTION.md rule 6).
"""

from __future__ import annotations

__all__ = ["REPO_ROOT", "FIXTURES_DIR"]

import pathlib

#: The `fixtures/` directory itself.
FIXTURES_DIR = pathlib.Path(__file__).resolve().parent

#: The repository root — `fixtures` is a top-level package resolved from it.
REPO_ROOT = FIXTURES_DIR.parent
