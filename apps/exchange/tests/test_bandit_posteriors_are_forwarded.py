"""The switch that turns durable learning on must be SET by somebody, in every deployment.

This repository's second-largest defect class is a variable read correctly in code and set by
nobody. ``proxyshop_support/tests/test_deploy_readiness.py`` carries the twin of this gate for
``PROXYSHOP_BUYER_STORE_WINDOW_TOKENS``, and records what it caught: a variable that appeared in
exactly three files repo-wide, was forwarded by **0 of 17 services across 10 compose fragments**,
and whose served route answered ``503 store-window-not-configured`` in every deployment that had
ever run, from a container reporting ``(healthy)``.

``EXCHANGE_BANDIT_POSTERIORS`` selects where the exposure bandit's Beta posteriors live. Unset,
the exchange keeps the process-local book every deployment ran before it existed — everything the
market learns is lost on restart, is not shared between replicas, and two uvicorn workers keep two
divergent models. There is no code-side default that turns durability on, so the compose fragment
IS the switch and a dropped forward is the whole feature going quietly back to how it was.

Three links, and each one alone leaves the hole open:

* **documented** in ``.env.example``, so the documented ``cp .env.example .env`` path produces a
  stack whose learning survives a restart;
* **forwarded** by ``apps/exchange/compose.yaml``, because compose forwards no host variable it
  does not name — an operator who exports this into their shell would otherwise change nothing
  about the container;
* **defaulted to a word the code accepts**, because a forward whose default is a typo is a
  forwarded variable that still does nothing. ``bandit_posteriors_from_env`` is asked directly
  rather than the word being compared against a list copied into this file.

There is deliberately **no healthcheck clause**, and that is the difference from the merchant
admin token's gate. An unreachable Redis here degrades to the in-memory book and the auction still
serves — one shortlist slot of four is affected, never rank and never price — so requiring it
would turn a legitimate, serving deployment into a red container.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from exchange.policy.durable import (
    BANDIT_BOOK_REDIS,
    ENV_BANDIT_POSTERIORS,
    RedisBanditPosteriors,
    bandit_posteriors_from_env,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
FRAGMENT = REPO_ROOT / "apps" / "exchange" / "compose.yaml"


def _exchange_service() -> dict[str, Any]:
    body = yaml.safe_load(FRAGMENT.read_text(encoding="utf-8")) or {}
    services = body.get("services") or {}
    service = services.get("exchange")
    assert isinstance(service, dict), (
        f"{FRAGMENT} no longer declares an `exchange` service, so this gate is measuring nothing"
    )
    return service


def _environment(service: dict[str, Any]) -> dict[str, str]:
    declared = service.get("environment") or {}
    if isinstance(declared, dict):
        return {str(key): str(value) for key, value in declared.items()}
    pairs = (str(entry).split("=", 1) for entry in declared)
    return {parts[0]: (parts[1] if len(parts) > 1 else "") for parts in pairs}


def test_the_durable_posterior_switch_is_documented_forwarded_and_a_word_the_code_accepts() -> None:
    """All three links at once, so a fix that restores one of them cannot look like a pass."""
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(rf"^{ENV_BANDIT_POSTERIORS}=", env_example, flags=re.MULTILINE), (
        f".env.example does not mention {ENV_BANDIT_POSTERIORS} at all, so an operator has no "
        f"documented way to reach the durable book and no way to learn that one exists."
    )

    environment = _environment(_exchange_service())
    assert ENV_BANDIT_POSTERIORS in environment, (
        f"apps/exchange/compose.yaml's exchange service does not forward "
        f"{ENV_BANDIT_POSTERIORS} into the container, so the composition root reads nothing "
        f"however the host is configured — which is how a feature comes to be unreachable in "
        f"every deployment while its tests pass."
    )

    declared = environment[ENV_BANDIT_POSTERIORS]
    _, _, default = declared.partition(":-")
    default = default.rstrip("}").strip()

    # STILL DEFAULTED OFF — but for a DIFFERENT reason than before, and the difference is the
    # point of this comment. The two defects this assertion used to name are CLOSED, measured on
    # the served path before and after at PROXYSHOP_WORKER=17, against a book of 512 pairs
    # sharing no cluster and no store (the worst case `DEFAULT_MAX_PAIRS` admits, and the shape
    # an anonymous caller gets to choose). `durable.py::_read` no longer materialises the CROSS
    # PRODUCT of every cluster held by every store held; it holds one cell per RECORDED pair,
    # which fixes both at once:
    #
    #   * the cross-product blowup. POST /auctions/{id}/accept: 262,144 cells and 675.2 ms
    #     BEFORE, 512 cells and 37.3 ms AFTER. POST /internal/outcomes: 262,144 cells and
    #     160.6 ms BEFORE, 512 cells and 4.7 ms AFTER — warm medians, measured the same way
    #     either side (~192 ms / ~17 ms cold). Pinned through the unauthenticated accept
    #     door by `test_the_durable_read_is_bounded_and_keeps_the_prior.py`.
    #   * the filler that flattened the trust-seeded prior. An absent pair is ABSENT now rather
    #     than `Posterior(1.0, 1.0)`, so `exploration.exposure_shares` leaves standing the prior
    #     `initial_state` seeded from the live trust snapshot. Measured on the `exploration`
    #     block a served POST /auctions publishes: one outcome for `store-y` in `cluster-9` moved
    #     `cluster-1`'s exploration slot from `store-x` to `store-y` BEFORE, and moves nothing
    #     AFTER. Pinned by that file's last test.
    #
    # WHAT BLOCKS ARMING NOW is not a served-path defect, it is that the flip makes the
    # DOCUMENTED SETUP PATH's own test suite red. `README.md`, `docs/deploy.md`,
    # `docs/demo/starting-slice.md` and `docs/demo/shopper-demo.md` all say `cp .env.example
    # .env`, and `scripts/verify.sh` sources `.env` — so `.env.example=redis` means everyone
    # following the runbook runs `make verify` with the durable book bound. Measured: the nine
    # files covering this book go from 200 passed to 8 failed / 188 passed / 4 errors, and the
    # whole exchange suite picks up 5 new failures. Three distinct causes, none of them fixed by
    # flushing:
    #
    #   1. `test_outcome_identifier_bound.py` asserts `len(book._stores) == DEFAULT_BANDIT_STORES`
    #      and `_book_bytes(...) == 0` for a refused outcome. Both read the IN-MEMORY book's
    #      internals; `RedisBanditPosteriors` has no `_stores`, and the composition root now
    #      always binds a book, so "a refused outcome leaves nothing behind" is false by
    #      construction. Those tests have to wire their own book, the way
    #      `test_durable_posteriors.durable_env` already pins `EXCHANGE_AUCTION_STORE=memory` so
    #      a developer's shell cannot change what a test measures.
    #   2. Cross-test state leaks: the durable book is keyed by `(cluster, store)` and the suite
    #      REUSES `cluster-1`/`store-x`/`store-y` across files, while the tests that reach it do
    #      not request the `redis_client` fixture and so never flush between them. That is
    #      order-dependence by construction.
    #   3. `e2e/test_learning.py` is 4-red under the durable book and 13-green under the
    #      in-memory one, because `RedisBanditPosteriors.state()`'s fail-closed
    #      `blacklisted=frozenset(stores)` makes every share it reads 0.0. That harness reads a
    #      book's state directly rather than through `exposure_shares`, so the fail-closed flags
    #      that never reach a served shortlist do reach it. This one is a real gap in the durable
    #      book, not a test-isolation problem.
    #
    # So: the read is fixed and verified, and turning this on is a SEPARATE piece of work that
    # has to reconcile those three first. Do not flip it to make a demo durable and discover
    # `make verify` is red.
    assert default == "", (
        f"apps/exchange/compose.yaml defaults {ENV_BANDIT_POSTERIORS} to {default!r}. The "
        f"durable book is defaulted OFF until the three blockers in this test's comment are "
        f"closed; if you closed them, say so here and change this assertion deliberately."
    )
    assert bandit_posteriors_from_env({ENV_BANDIT_POSTERIORS: default}) is None, (
        "an empty selector must resolve to nothing at all, so `bind_bandit_posteriors` returns "
        "before it touches the app and the lazily-created process-local book is unchanged"
    )

    # Asked of the reader rather than compared against a list copied into this file, so a
    # renamed word cannot leave a green gate in front of a selector nothing accepts. The book
    # is KEPT, not deleted, and this is what says so.
    chosen = bandit_posteriors_from_env({ENV_BANDIT_POSTERIORS: BANDIT_BOOK_REDIS})
    assert isinstance(chosen, RedisBanditPosteriors), (
        f"{ENV_BANDIT_POSTERIORS}={BANDIT_BOOK_REDIS!r} is answered with "
        f"{type(chosen).__name__} rather than the durable book, so the switch an operator is "
        f"told to use does not reach it."
    )


def test_the_env_example_and_the_compose_default_do_not_disagree() -> None:
    """Two spellings of "which datastore" is how a stack ends up half-durable.

    ``.env.example`` is copied verbatim by the documented setup path, so its value IS the
    default for everyone who follows the runbook; the compose default is what an operator who
    never made a ``.env`` gets. A stack where those two disagree is one where "did my learning
    survive?" depends on a file the operator may not know exists.
    """
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(rf"^{ENV_BANDIT_POSTERIORS}=(.*)$", env_example, flags=re.MULTILINE)
    assert match is not None
    documented = match.group(1).strip()

    declared = _environment(_exchange_service())[ENV_BANDIT_POSTERIORS]
    _, _, compose_default = declared.partition(":-")

    assert documented == compose_default.rstrip("}").strip(), (
        f".env.example says {ENV_BANDIT_POSTERIORS}={documented!r} and "
        f"apps/exchange/compose.yaml defaults it to {compose_default!r}"
    )


def test_the_variable_is_read_by_the_composition_root_and_not_only_by_its_own_module() -> None:
    """A forward into a container nothing consumes is the same dead feature wearing a name.

    The reader has to be on the path ``ensure_configured`` takes, and it has to be on BOTH of
    that function's branches — the shipped container has no deployment document of its own to
    edit, so a switch that only worked on the document branch would be unreachable from the one
    deployment this repository actually ships (the ``EXCHANGE_SHOP_ROSTER`` trap, which
    ``apps/exchange/compose.yaml`` records in its own words as "⚠️ IT DOES NOTHING ON ITS OWN").
    """
    composition = (REPO_ROOT / "apps" / "exchange" / "src" / "composition.py").read_text(
        encoding="utf-8"
    )
    assert composition.count("bind_bandit_posteriors(app,") == 2, (
        "composition.py does not call bind_bandit_posteriors on both of ensure_configured's "
        "branches; a deployment with no document would keep the process-local book."
    )
