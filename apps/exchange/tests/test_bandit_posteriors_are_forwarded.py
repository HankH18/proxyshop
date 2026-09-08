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

    # DEFAULTED OFF, and this assertion is the record of why rather than a preference. Two
    # defects were measured on the SERVED path of the durable book and neither is closed:
    #
    #   * `durable.py`'s read materialises the CROSS PRODUCT of every cluster by every store it
    #     holds while `DEFAULT_MAX_PAIRS` bounds the recorded PAIRS — measured at 262,144 cells
    #     and 178 ms per served outcome, reached through the unauthenticated
    #     `POST /auctions/{id}/accept` door and surviving a restart for the 30-day expiry;
    #   * the filler for a pair nobody recorded is `Posterior(1.0, 1.0)`, which is what
    #     `bandit.initial_state` seeds ONLY where the trust snapshot says nothing. Where it
    #     says something, the durable read flattens the trust-seeded prior on the ranking path
    #     and lets an outcome in one cluster move another cluster's exposure.
    #
    # Turning it back on is therefore a deliberate act that has to come through this test and
    # read that list. Empty is the OLD behaviour and not a broken switch: the assertions below
    # pin both halves of that — empty resolves to nothing at all, and the word still builds the
    # book — so "off" cannot quietly become "the selector stopped working".
    assert default == "", (
        f"apps/exchange/compose.yaml defaults {ENV_BANDIT_POSTERIORS} to {default!r}. The "
        f"durable book is defaulted OFF until the two defects in this test's comment are "
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
