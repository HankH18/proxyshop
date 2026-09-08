"""``apps.buyer.seed`` — the seeded buyer population behind ``app.buyer_accounts`` (T-142).

The ticket, in one paragraph
============================
``app.buyer_accounts`` is written on every served ``GET /buyer/profile`` and, until this
package existed, **read by nobody**: no service, no route, no script anywhere in the tree ever
issued a ``SELECT`` against it, and ``app.intents.pseudonym``'s foreign key onto it was inert
because nothing writes ``app.intents`` either. The two ways to close that are to delete the
write or to build the consumer. This is the third: the table is what a store is *meant* to be
shown, so it is populated with realistic buyers and given a real, served reader, and going
live becomes a switch rather than a rewrite.

1. It is a script, not a service
=================================
Nothing here is imported by anything under ``apps/buyer/svc/src/``, nothing here is in the
buyer image's ``COPY`` set (``apps/buyer/Dockerfile`` copies ``apps/buyer/svc`` and this is its
sibling), and the served route knows nothing about it. The import edge is one-way —
``apps.buyer.seed`` → ``buyer_svc.profile`` and ``buyer_svc.vault``, never back — and
``test_the_served_window_has_no_notion_of_seeding`` asserts that the served directory contains
no mention of seeding at all.

The **output** is the artefact. ``apps/buyer/seed-data/store-window/`` is committed, carries
the sha256 of its own bytes, records the exact command that reproduces them, and is refused by
:func:`~apps.buyer.seed.store.load` if either has moved.

2. Seeded and real, distinguishable forever
============================================
``services/sim/seed`` put its marker inside the ledger's hash chain, where the platform's own
``event_hash`` covers it. A table has no chain, so the equivalent is built out of what a table
does have — see :mod:`apps.buyer.seed.chain` for the argument in full, but in short:

* the marker is the **PRIMARY KEY**: a seeded row's pseudonym is ``psn-seed-`` + 24 hex, and
  the vault mints ``psn-`` + 32 characters of ``secrets.token_hex``, in which ``s`` never
  appears — so no buyer, by logging in any number of times, can produce a row in the seeded
  namespace;
* the key is a **hash chain over the corpus**, so the committed seeded set is pinned to a
  digest and cannot be extended, reordered or edited without moving it;
* the database **refuses disagreement**: ``0005_buyer_accounts_provenance.sql`` carries
  ``CHECK ((provenance = 'seed') = (pseudonym LIKE 'psn-seed-%'))`` as an equivalence, so
  neither forgery is a rule this code enforces and could forget to enforce.

**And one thing those do not cover, which is stated here rather than left to be discovered.**
Together they guarantee *consistency* — no caller of any served route can produce a row that
reads as seeded, or un-mark one that is. They do not guarantee *membership*: the table stores
no signature, so anything holding the ``app`` role can insert ``psn-seed-`` plus any
twenty-four characters and that row is legal, is served as seeded, and is in no chain.
``python -m apps.buyer.seed audit --dsn <dsn>`` is the check that closes it — it compares the
table's seeded rows against the committed corpus and exits non-zero on any that is unpinned,
altered or missing. Run it after ``load``; the ``load`` receipt says so.

A consequence worth stating on its own: **seeded buyers have no entry in
``vault.pseudonym_history``.** Their pseudonyms were minted by a digest, not issued by the
vault, so there is nothing anywhere — for any role, including ``buyer_vault`` — that resolves
one to a person. R5 holds for the seeded half of the window by construction.

3. Who may read the window, and through which door
===================================================
D5's grant model already answers the first half: ``exchange``, ``trust_rw`` and ``buyer_vault``
hold ``SELECT`` on ``app.*``, and exactly one role — ``buyer_vault`` — reaches ``vault.*``. The
store side of the network is *entitled* to this table and is denied the mapping, and
``apps/buyer/svc/tests/test_auth_vault.py::test_the_store_facing_row_is_readable_while_the_mapping_is_not``
has pinned that pair for as long as the table has existed.

What was missing was a **door**. The entitlement was a database grant with no HTTP route
behind it: the exchange holds the credential and has no Postgres client at all, and the buyer
service's only profile route is gated on ``X-Buyer-Session``, which one buyer holds and which
must never be widened into "and also every other buyer". So the reader is a new door rather
than a wider one: :mod:`buyer_svc.window.routes` serves ``GET /buyer/store-window`` to a
store-scoped bearer token, over the ``app``-role connection the service already opens — a role
with **no USAGE on schema vault**, so the route cannot resolve a pseudonym to a person even if
its SQL were rewritten to try.

4. The live switch — and what it actually is
=============================================
**The seam is ``app.buyer_accounts`` itself, and there is nothing else.**

``GET /buyer/store-window`` selects ``pseudonym, buckets, provenance`` and copies the column it
finds. It has no branch on seeding, no flag, no debug mode, and no import from this package.
Real logins publish into the same table through :func:`buyer_svc.profile.publish_profile`, and
the route cannot tell which door a row came through — the database can, and says so in a
column the route passes on.

So going live is:

* **server side: nothing.** Not a flag, not a config value, not a branch.
* **operationally: two commands.** Stop running ``python -m apps.buyer.seed load``, and
  ``delete from app.buyer_accounts where provenance = 'seed'``. That statement is exact and
  safe *because* of the guarantee in §2 — it cannot match a real buyer's row, and it cannot
  miss a seeded one.

Usage::

    python -m apps.buyer.seed run                                  # produce the corpus
    python -m apps.buyer.seed verify                               # recompute its chain
    python -m apps.buyer.seed load  --dsn $PROXYSHOP_PG_DSN_APP    # put it in the table
    python -m apps.buyer.seed audit --dsn $PROXYSHOP_PG_DSN_APP    # is the table still ours?
    curl -H 'Authorization: Bearer <store token>' localhost:8081/buyer/store-window
"""

from __future__ import annotations

__all__ = ["__doc__"]
