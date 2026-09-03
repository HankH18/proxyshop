"""Two confirmed R5 disclosure defects in the buyer profile path (T-139, T-133 item a).

Both are about the *same* boundary from opposite sides: the one place a buyer's identity can
reach a store, and the guard that is supposed to notice when it does.

T-139 — the one bucket the backstop cannot see into
---------------------------------------------------
Four of the five facets on ``BuyerProfile.buckets`` are drawn from a closed vocabulary the
coarseners own. The fifth, ``category_affinity``, is open: it carries slugs of the account's
*own* ``orders[].category`` strings. ``identity_leaks`` therefore held every one of those
slugs out of the haystack entirely — which is right for the case it was written for (a buyer
who lives on Park Lane and buys ``park-gear`` must not be locked out forever) and wrong in
general, because ``orders[].category`` is free text nobody validates. Put an address in it::

    {"category": "gift for Dana Reyes, 44 Alder Way Portland 97205"}

and the buyer's first name, last name, street and postal code are published verbatim into the
store-facing profile while the backstop reports clean. Not live today only because nothing yet
writes orders into an account; the buyer flow that will (T-071/072/073) is scheduled.

The fix has to hold both directions at once, and both are asserted below:

* **refuse** a slug that is not a merchandising slug at all, or one built out of the buyer's
  name — ``dana-reyes``, ``park-lane`` — because there the collision is the disclosure;
* **admit** the two documented collisions the exemption exists for — ``none@example.com``
  whose brand-new profile says ``frequency_tier == "none"``, and ``espresso.fan@example.com``
  who buys espresso — plus the Park Lane repro, because a backstop that refuses ordinary
  signups is a denial of service wearing a privacy guarantee.

T-133 (a) — the leak detector was itself a disclosure path
----------------------------------------------------------
``IdentityLeak`` interpolated the leaked identity *values* into its message, ``read_profile``
caught only ``SessionError``, so the one exception that fires **because** identity escaped
left the process as a 500 traceback carrying that identity. ``NotAPseudonym`` did the same
with the subject it refused — and the realistic mistake it exists to catch is
``sessions.open(email)``, so the value it echoed was an email address.

T-133 (b) — the unauthenticated session table
---------------------------------------------
``InMemorySessionStore`` had no ceiling and swept nothing, so sessions opened and never
revisited lived for the life of the process. Bounded here the way ``request_login`` already
bounds its own table: sweep what is dead, then refuse rather than evict somebody live.

Every product import happens inside a test body, matching the rest of this suite.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

#: The repro, exactly as triage recorded it: an ordinary account whose order category field
#: was used as a gift note. Nothing here is malformed — ``orders[].category`` is free text and
#: no code path validates it — and every identity fragment in that note is a real one from the
#: same record, which is what makes the published slug a disclosure rather than a coincidence.
CONTAMINATED = {
    "account_id": "acct-9f3c21",
    "email": "dana.reyes@example.com",
    "first_name": "Dana",
    "last_name": "Reyes",
    "phone": "+1-555-0100",
    "address": "44 Alder Way, Portland OR 97205",
    "postal_code": "97205",
    "region": "US-OR",
    "budget_band": "100-250",
    "orders": [
        {
            "order_ref": "ord-1",
            "total": 118.0,
            "category": "gift for Dana Reyes, 44 Alder Way Portland 97205",
        },
        {"order_ref": "ord-2", "total": 232.5, "category": "running-shoes"},
        {"order_ref": "ord-3", "total": 96.0, "category": "running-shoes"},
    ],
}

#: The identity fragments of :data:`CONTAMINATED` that ride out inside that slug.
SMUGGLED = ("dana", "reyes", "alder", "portland", "97205")

#: The Park Lane repro, restated here so this file stands on its own: an ordinary street name
#: that is also a token of an ordinary category slug. Nothing disclosive reaches the profile
#: and the buyer must be able to log in, forever, without changing their address.
PARK_LANE = {
    "email": "l.hunt@example.com",
    "first_name": "Lena",
    "last_name": "Hunt",
    "address": "12 Park Lane, Boulder CO 80301",
    "postal_code": "80301",
    "region": "US-CO",
    "orders": [
        {"order_ref": "ord-1", "total": 140.0, "category": "park-gear"},
        {"order_ref": "ord-2", "total": 210.0, "category": "trail-gear"},
        {"order_ref": "ord-3", "total": 175.0, "category": "park-gear"},
    ],
}

PSEUDONYM = "psn-" + "ab" * 16


def _blob(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True).casefold()


# =======================================================================================
# T-139 — identity must not ride out inside a category slug
# =======================================================================================


def test_an_address_in_a_category_field_does_not_reach_the_profile() -> None:
    """The recorded repro. ``build_profile`` must refuse rather than publish the note."""
    from buyer_svc.profile import IdentityLeak, build_profile

    with pytest.raises(IdentityLeak):
        build_profile(CONTAMINATED, PSEUDONYM)


def test_the_backstop_reports_every_fragment_that_rode_out_in_a_slug() -> None:
    """And it reports them as a *leak*, not as an empty list.

    Driven through ``identity_leaks`` directly as well as through the builder: the builder
    could be made to refuse for some unrelated reason, and the point of the ticket is that
    the detector itself could not see into ``category_affinity``.
    """
    from buyer_svc.profile import build_buckets, identity_leaks

    profile = {"pseudonym": PSEUDONYM, "buckets": build_buckets(CONTAMINATED).model_dump()}
    leaked = identity_leaks(profile, CONTAMINATED)
    assert set(SMUGGLED) <= set(leaked), (
        f"the buyer's name, street and postal code reached {profile['buckets']} and the "
        f"backstop reported {leaked}"
    )


def test_a_slug_built_only_out_of_the_buyers_name_is_refused() -> None:
    """The short smuggle the length and digit caps alone would wave through.

    ``dana-reyes`` is a perfectly well-formed merchandising slug: nine characters, two
    tokens, no digits. It is also the buyer's full name, and it is the account's own
    category, so the account-slug exemption used to publish it. Same for a slug spelling the
    street the buyer lives on.
    """
    from buyer_svc.profile import IdentityLeak, build_profile

    for smuggled in ("Dana Reyes", "dana gear", "Alder Way Portland"):
        account = dict(CONTAMINATED)
        account["orders"] = [
            {"order_ref": f"o{n}", "total": 30.0, "category": smuggled} for n in range(3)
        ]
        with pytest.raises(IdentityLeak, match="R5"):
            build_profile(account, PSEUDONYM)


def test_a_category_slug_is_exempt_only_inside_the_affinity_bucket() -> None:
    """The exemption was account-wide; a slug found in ``region`` was exempt too.

    ``category_affinity`` is the only bucket a category slug has any business appearing in.
    A coarsener rewired to copy one into ``region`` — the exact shape of the rewiring the
    suite already tests for with a postal code — was silently exempt, because the value
    equalled one of the account's own slugs.
    """
    from buyer_svc.profile import identity_leaks

    smuggled = {"pseudonym": "psn-x", "buckets": {"region": "park-gear"}}
    assert identity_leaks(smuggled, PARK_LANE) == ["park"], (
        "a category slug copied into `region` is exempt from the identity backstop"
    )


def test_an_account_with_no_name_fields_cannot_reassemble_its_email_in_a_slug() -> None:
    """Found by trying to smuggle a fragment through a route the first fix did not cover.

    Every account above carries ``first_name``/``last_name``, and the rule that refuses
    ``dana-reyes`` leans on them. But :meth:`MagicLinkAuth.redeem` creates a first-time
    account as ``{"email": email}`` and nothing else — no name, no address — which is the
    shape T-071/072/073 will be hanging orders off. On that account ``"dana"`` and
    ``"reyes"`` are reachable only through the email, the leniency that lets
    ``espresso.fan@example.com`` buy espresso applied, and ``dana-reyes`` was published.

    A one-token collision with a self-chosen address is a coincidence. A multi-token slug
    whose every token is one is the local part being reassembled.
    """
    from buyer_svc.profile import IdentityLeak, build_profile

    reassembled = {
        "email": "dana.reyes@example.com",
        "orders": [
            {"order_ref": f"o{n}", "total": 30.0, "category": "dana reyes"} for n in range(3)
        ],
    }
    with pytest.raises(IdentityLeak) as caught:
        build_profile(reassembled, PSEUDONYM)
    assert caught.value.account_keys == ("email",)

    # And the coincidence it must not have cost: one token, same account shape.
    espresso = {
        "email": "espresso.fan@example.com",
        "orders": [{"order_ref": f"o{n}", "total": 12.0, "category": "espresso"} for n in range(3)],
    }
    assert build_profile(espresso, PSEUDONYM).model_dump()["buckets"]["category_affinity"] == [
        "espresso"
    ]


# =======================================================================================
# T-139 — the other direction: the denial of service the exemption exists to stop
# =======================================================================================


def test_a_brand_new_signup_whose_email_names_a_bucket_value_still_builds() -> None:
    """``none@example.com`` has no orders, so ``frequency_tier == "none"``."""
    from buyer_svc.profile import build_profile

    built = build_profile({"email": "none@example.com", "orders": []}, PSEUDONYM).model_dump()
    assert built["buckets"]["frequency_tier"] == "none"


def test_a_buyer_who_buys_what_their_email_is_named_after_still_builds() -> None:
    """``espresso.fan@example.com`` buying espresso. One bucket over, same collision."""
    from buyer_svc.profile import build_profile

    espresso = {
        "email": "espresso.fan@example.com",
        "orders": [{"order_ref": f"o{n}", "total": 12.0, "category": "espresso"} for n in range(3)],
    }
    built = build_profile(espresso, PSEUDONYM).model_dump()
    assert built["buckets"]["category_affinity"] == ["espresso"]
    assert built["buckets"]["frequency_tier"] == "regular"


def test_a_buyer_whose_street_names_a_category_still_builds() -> None:
    """Park Lane buying park-gear; the repro the exemption was added for."""
    from buyer_svc.profile import build_profile, identity_leaks

    built = build_profile(PARK_LANE, PSEUDONYM).model_dump()
    assert built["buckets"]["category_affinity"] == ["park-gear", "trail-gear"]
    assert identity_leaks(built, PARK_LANE) == []


def test_a_surname_that_is_a_prefix_of_a_category_still_builds() -> None:
    """Cook buying cookware, Hunt buying hunting-gear. Substrings, not slugs."""
    from buyer_svc.profile import build_profile

    for surname, category in (("Cook", "cookware"), ("Hunt", "hunting-gear")):
        account = {
            "email": f"{surname}.buyer@example.com".casefold(),
            "last_name": surname,
            "orders": [
                {"order_ref": f"o{n}", "total": 40.0, "category": category} for n in range(3)
            ],
        }
        build_profile(account, PSEUDONYM)  # must not raise


def test_the_residual_one_token_collisions_are_admitted_on_purpose() -> None:
    """What the fix deliberately does **not** refuse, written down so it cannot drift.

    Three shapes stay published, and all three are the Park Lane / Cook precedent rather
    than an oversight: a single identity token beside a genuine merchandising one
    (``park-gear``, ``dana-gear``), a fragment that is a substring but not a token
    (``cookware`` for a buyer named Cook, ``danaware`` for one named Dana), and a one-token
    slug colliding with a word of a self-chosen email (``espresso``).

    Refusing any of them means refusing the buyer forever, for as long as their name and
    their shopping habits both stay what they are. If a future change makes one of these
    raise, the trade was re-made — deliberately or not — and this test is where that shows.
    """
    from buyer_svc.profile import build_profile

    named = {
        "email": "dana.reyes@example.com",
        "first_name": "Dana",
        "last_name": "Reyes",
        "address": "44 Alder Way, Portland OR 97205",
        "postal_code": "97205",
    }
    for account, category in (
        (named, "danaware"),
        (named, "alder gear"),
        ({"email": "dana.reyes@example.com"}, "dana"),
        ({"email": "dana.reyes@example.com"}, "dana gear"),
    ):
        record = dict(account)
        record["orders"] = [
            {"order_ref": f"o{n}", "total": 30.0, "category": category} for n in range(3)
        ]
        build_profile(record, PSEUDONYM)  # admitted, and that is the documented trade


def test_the_generated_directory_still_builds_end_to_end() -> None:
    """A population, not a fixture: a tightened backstop must not start refusing at random."""
    from buyer_svc.profile import build_profile

    givens = ("Dana", "Samir", "Priya", "Tomas", "Ingrid", "Kwame")
    surnames = ("Reyes", "Okafor", "Lindqvist", "Marchetti", "Nakamura", "Boateng")
    categories = ("running-shoes", "trail-gear", "espresso", "cookware", "headphones", "tents")
    for index in range(240):
        given = givens[index % len(givens)]
        surname = surnames[(index // 2) % len(surnames)]
        account = {
            "email": f"{given}.{surname}{index}@example.invalid".casefold(),
            "first_name": given,
            "last_name": surname,
            "address": f"{index + 10} Alder Way, Portland",
            "postal_code": f"{90000 + index}",
            "region": "US-OR",
            "orders": [
                {
                    "order_ref": f"ord-{index}-{n}",
                    "total": 40.0 + n,
                    "category": categories[(index + n) % len(categories)],
                }
                for n in range(index % 5)
            ],
        }
        build_profile(account, f"psn-case-{index:04d}")  # must not raise


# =======================================================================================
# T-133 (a) — the detector that exists to protect R5 must not itself disclose
# =======================================================================================


def test_the_leak_report_names_the_account_keys_and_never_the_values() -> None:
    """``IdentityLeak`` used to interpolate the leaked values. That is the disclosure."""
    from buyer_svc.profile import IdentityLeak, build_profile

    with pytest.raises(IdentityLeak) as caught:
        build_profile(CONTAMINATED, PSEUDONYM)
    message = str(caught.value).casefold()

    for fragment in SMUGGLED:
        assert fragment not in message, f"{fragment!r} was interpolated into {message!r}"
    assert CONTAMINATED["email"] not in message
    assert PSEUDONYM.casefold() not in message
    # It still has to be actionable: name the account keys that leaked, not the values.
    assert "first_name" in message and "postal_code" in message, message


def test_the_profile_route_answers_a_leak_without_disclosing_it() -> None:
    """``GET /buyer/profile`` raised ``IdentityLeak`` straight through FastAPI as a 500.

    ``read_profile`` caught only ``SessionError``, so the response body — and every
    traceback rendered from it — carried the buyer's name, street and postal code out to an
    unauthenticated network peer. Driven through the app a deployment actually boots.
    """
    from buyer_svc.auth import MagicLinkAuth
    from buyer_svc.auth.magic_link import InMemoryAccountDirectory
    from buyer_svc.auth.routes import get_auth_service
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    tokens: list[str] = []
    service = MagicLinkAuth(
        deliver=lambda email, token, expires_at: tokens.append(token),
        accounts=InMemoryAccountDirectory({CONTAMINATED["email"]: CONTAMINATED}),
    )
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False)

    assert (
        client.post("/buyer/auth/magic-link", json={"email": CONTAMINATED["email"]}).status_code
        == 202
    )
    created = client.post("/buyer/auth/session", json={"token": tokens[-1]})
    assert created.status_code == 201, created.text

    fetched = client.get(
        "/buyer/profile", headers={"X-Buyer-Session": created.json()["session_id"]}
    )
    assert fetched.status_code == 500, fetched.text
    body = fetched.text.casefold()
    for fragment in (*SMUGGLED, CONTAMINATED["email"], CONTAMINATED["phone"]):
        assert fragment.casefold() not in body, (
            f"{fragment!r} reached an HTTP response body: {body}"
        )


def test_refusing_a_session_subject_does_not_echo_it() -> None:
    """``NotAPseudonym`` exists to catch ``sessions.open(email)`` — and printed the email."""
    from buyer_svc.auth import InMemorySessionStore, NotAPseudonym

    sessions = InMemorySessionStore()
    for refused in ("dana.reyes@example.com", "acct-9f3c21", "Dana Reyes"):
        with pytest.raises(NotAPseudonym) as caught:
            sessions.open(refused)
        assert refused.casefold() not in str(caught.value).casefold(), str(caught.value)
        assert "pseudonym" in str(caught.value).casefold()


# =======================================================================================
# T-133 (b) — the unauthenticated session table must stay bounded
# =======================================================================================


def test_the_session_store_sweeps_the_dead_and_refuses_rather_than_evicting() -> None:
    """A session opened and never revisited used to live for the life of the process.

    Expiry was only ever noticed by ``get``, so nothing swept. The ceiling refuses rather
    than evicting for the same reason ``request_login`` does: evicting the oldest would let
    an unauthenticated caller end a chosen victim's session on demand.
    """
    from buyer_svc.auth.sessions import InMemorySessionStore, SessionsExhausted
    from buyer_svc.vault import PseudonymVault

    now = {"t": datetime(2026, 3, 1, 12, 0, tzinfo=UTC)}
    vault = PseudonymVault()
    sessions = InMemorySessionStore(clock=lambda: now["t"], ttl=timedelta(hours=1), max_sessions=8)

    for index in range(8):
        sessions.open(vault.issue(f"buyer{index}@example.com"))
    assert len(sessions) == 8

    # Full: the ninth is refused, and the eight already held are untouched.
    with pytest.raises(SessionsExhausted):
        sessions.open(vault.issue("late@example.com"))
    assert len(sessions) == 8

    # Time passes; the dead are swept on the next open rather than waiting for a lookup.
    now["t"] += timedelta(hours=2)
    live = sessions.open(vault.issue("fresh@example.com"))
    assert len(sessions) == 1
    assert not live.expired(now["t"])


def test_a_session_still_carries_no_identity_after_the_ceiling_landed() -> None:
    """The bound must not have been bought by putting the subject somewhere new."""
    from buyer_svc.auth import MagicLinkAuth

    tokens: list[str] = []
    auth = MagicLinkAuth(deliver=lambda email, token, expires_at: tokens.append(token))
    auth.request_login("dana.reyes@example.com")
    session = auth.redeem(tokens[-1])
    blob = _blob(vars(session))
    for secret in ("dana", "reyes", "example.com"):
        assert secret not in blob


# =======================================================================================
# T-189 — one merchandise token must not launder an unbounded pile of identity
# =======================================================================================


def test_a_name_less_account_cannot_publish_its_name_beside_one_merchandise_word() -> None:
    """The recorded T-189 repro: append ``gear`` and the T-139 fix stops applying.

    ``_exempt_category_slugs`` rule 3 asks whether the slug has *at least one* token that is
    merchandise rather than the buyer. It never asks how many tokens are the buyer. So the
    account ``MagicLinkAuth.redeem`` creates on a first login — ``{"email": email}``, no
    ``first_name``, no ``last_name``, which is the shape T-071/072/073 hangs orders off —
    publishes ``dana-reyes-gear`` to the store and ``identity_leaks`` reports clean:

    * rule 2 has nothing to work with, because ``"dana"`` and ``"reyes"`` reach the account
      only through ``email``, which is deliberately not a naming key;
    * rule 3 is satisfied by ``"gear"`` alone, and the two name tokens ride out beside it.

    ``dana-reyes`` on the very same account is refused (asserted above). Appending one
    innocuous word is the whole of the bypass.
    """
    from buyer_svc.profile import IdentityLeak, build_profile

    redeemed = {
        "email": "dana.reyes@example.com",
        "orders": [
            {"order_ref": f"o{n}", "total": 30.0, "category": "Dana Reyes gear"} for n in range(3)
        ],
    }
    with pytest.raises(IdentityLeak) as caught:
        build_profile(redeemed, PSEUDONYM)
    assert caught.value.account_keys == ("email",)


def test_padding_identity_with_merchandise_is_refused_on_every_route_it_reaches() -> None:
    """The same bypass, driven through the four shapes an adversarial pass found.

    Each is a well-formed merchandising slug — under the character cap, under the token cap,
    no digit run — carrying **two or more** of the account's own identity fragments plus at
    least one word that is not one. Rule 3 waves every one of them through today.

    * ``dana reyes running shoes`` — the repro at the token cap rather than at three tokens;
    * ``danareyes gear`` — the same two fragments run together, so no *token* equals either
      of them and a token-wise rule never fires;
    * ``espresso fan gear`` — the email local part of the very account the one-token
      leniency exists for, reassembled;
    * ``Alder Way Portland gear`` — not a name-less account at all: on the fully populated
      T-139 record, ``Alder Way Portland`` is refused (asserted above) and appending
      ``gear`` publishes the buyer's street and city.

    A single collision is the coincidence the exemption exists for. Two is an assembly, and
    no number of merchandise words beside it makes it one again.
    """
    from buyer_svc.profile import IdentityLeak, build_profile

    named = {
        "email": "dana.reyes@example.com",
        "first_name": "Dana",
        "last_name": "Reyes",
        "address": "44 Alder Way, Portland OR 97205",
        "postal_code": "97205",
        "region": "US-OR",
    }
    for account, category in (
        ({"email": "dana.reyes@example.com"}, "dana reyes running shoes"),
        ({"email": "dana.reyes@example.com"}, "danareyes gear"),
        ({"email": "espresso.fan@example.com"}, "espresso fan gear"),
        (named, "Alder Way Portland gear"),
        (PARK_LANE, "park lane gear"),
    ):
        record = dict(account)
        record["orders"] = [
            {"order_ref": f"o{n}", "total": 30.0, "category": category} for n in range(3)
        ]
        with pytest.raises(IdentityLeak, match="R5"):
            build_profile(record, PSEUDONYM)


def test_identity_split_across_several_slugs_is_refused_as_one_disclosure() -> None:
    """Found by attacking the T-189 fix: the budget was per slug, the profile is a list.

    ``category_affinity`` publishes up to ``CATEGORY_LIMIT`` slugs, and each one earned the
    exemption on its own. So the fragment budget that refuses ``dana-reyes-gear`` in one slug
    was satisfied twice over by two orders::

        [{"category": "dana gear"}, {"category": "reyes gear"}]
        -> category_affinity == ["dana-gear", "reyes-gear"]

    The buyer's full name reaches the store exactly as it did before, spelled across two
    values instead of inside one. Same for the two halves of a street (``alder-gear`` and
    ``portland-gear``) and for the Park Lane buyer's own (``park-gear`` and ``lane-gear``),
    which is the sharpest of the three: ``park-gear`` alone is the collision the exemption
    was built for, and it stops being a collision the moment ``lane-gear`` is published
    beside it.

    A disclosure is a property of the profile, not of one value in it.
    """
    from buyer_svc.profile import IdentityLeak, build_profile

    named = {
        "email": "dana.reyes@example.com",
        "first_name": "Dana",
        "last_name": "Reyes",
        "address": "44 Alder Way, Portland OR 97205",
        "postal_code": "97205",
        "region": "US-OR",
    }
    for account, categories in (
        ({"email": "dana.reyes@example.com"}, ("dana gear", "reyes gear")),
        (named, ("alder gear", "portland gear")),
        (PARK_LANE, ("park gear", "lane gear")),
    ):
        record = dict(account)
        record["orders"] = [
            {"order_ref": f"o{index}-{n}", "total": 30.0, "category": category}
            for index, category in enumerate(categories)
            for n in range(3)
        ]
        with pytest.raises(IdentityLeak, match="R5"):
            build_profile(record, PSEUDONYM)


def test_the_park_lane_buyer_still_builds_beside_ordinary_merchandise() -> None:
    """The other half of the release-level budget: one collision is still one collision.

    The list is what is measured, so this is the case that proves the measure did not just
    become "refuse any account whose profile carries a fragment at all". Park Lane buys
    ``park-gear`` alongside two categories that have nothing to do with them, and the
    published list still spells exactly one word of their address.
    """
    from buyer_svc.profile import build_profile, identity_leaks

    record = dict(PARK_LANE)
    record["orders"] = [
        {"order_ref": f"o{index}-{n}", "total": 30.0, "category": category}
        for index, category in enumerate(("park-gear", "trail-gear", "camera-lenses"))
        for n in range(3)
    ]
    built = build_profile(record, PSEUDONYM).model_dump()
    assert built["buckets"]["category_affinity"] == ["camera-lenses", "park-gear", "trail-gear"]
    assert identity_leaks(built, record) == []
