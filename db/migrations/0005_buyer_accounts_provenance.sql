-- 0005 — `app.buyer_accounts.provenance`: seeded rows stay seeded, forever. Owned by T-142.
--
-- WHY THIS FILE EXISTS
-- ===================
-- `app.buyer_accounts` is the store-visible window onto a buyer: a pseudonym and five coarse
-- buckets, with the email side of the pair sealed in `vault.*` where only `buyer_vault`
-- reaches it. The window is served by `GET /buyer/store-window`
-- (apps/buyer/svc/src/window/routes.py), and it is populated from two sources at once:
--
--   * real logins  -- `buyer_svc.profile.publish_profile`, on every served `GET /buyer/profile`
--   * seeded rows  -- `python -m apps.buyer.seed load`, from the checked-in corpus under
--                     `apps/buyer/seed-data/store-window/`
--
-- A demonstrable window needs both, and a window a store can trust needs the two to be
-- **permanently** told apart. The precedent is `services/sim/seed/`, which put its marker
-- inside the ledger's hash chain so an un-marked seeded row and a marked organic one both
-- break `GET /events/verify`. A table is not a chain -- rows here are upserted in place and
-- there is no `prev_hash` to break -- so the equivalent guarantee is built from the two
-- things this table does have: a PRIMARY KEY the writer does not choose freely, and a
-- database that refuses statements.
--
-- THE GUARANTEE, IN TWO HALVES
-- ============================
-- (1) `provenance` says which source a row came from, and `buyer_accounts_provenance_is_the_key`
--     ties that claim to the PRIMARY KEY in BOTH directions:
--
--         provenance = 'seed'  <->  pseudonym LIKE 'psn-seed-%'
--
--     A live row cannot be re-labelled 'seed' (its vault pseudonym is
--     `psn-` + 32 HEX characters, and 's' is not a hex digit, so it can never match the
--     pattern). A seeded row cannot be re-labelled 'live' without changing the PRIMARY KEY,
--     which is not an UPDATE any writer in this tree issues and which would break the
--     corpus digest that pins it. Neither forgery is a policy this code enforces and could
--     forget to enforce -- both are statements Postgres rejects, for every writer, including
--     ones written after this file.
--
-- (2) The seeded half of the key is itself a hash chain. `apps/buyer/seed/chain.py` mints
--     the i-th seeded pseudonym as
--
--         psn-seed- || sha256(previous_digest || canonical_json({pseudonym_ordinal, buckets}))[:24]
--
--     so the marker is not a label BESIDE the data, it is a digest OF the data, and the
--     chain head is recorded in the corpus's `collection.json`. Editing a seeded row's
--     buckets, inserting a row into the middle, or appending one all move a digest that is
--     checked into this repository -- IN THE CORPUS FILE.
--
-- WHAT THIS FILE DOES NOT GUARANTEE, said plainly
-- ===============================================
-- The constraints above are about CONSISTENCY: no row can exist whose label and whose key
-- disagree, so no caller of any served route -- a real buyer logging in included -- can
-- produce a row that reads as seeded, or strip the marker from one that is.
--
-- They say nothing about MEMBERSHIP. This table stores no signature, so anything holding the
-- `app` role may `insert (pseudonym, buckets, provenance) values ('psn-seed-' || <any 24
-- characters>, ..., 'seed')`, and that row is legal, is served by `GET /buyer/store-window` as
-- seeded, and matches no link in any chain. Symmetrically, manufactured buckets written under
-- a fresh `psn-<32 hex>` key are indistinguishable from a real buyer's row: there is no FK
-- into `vault.pseudonym_history`, and there cannot be one that the reading roles could check,
-- because D5 denies them the vault entirely.
--
-- The check that closes membership is `python -m apps.buyer.seed audit --dsn <dsn>`, which
-- compares this table's seeded rows against the committed corpus and exits non-zero on any
-- row that is unpinned, altered or missing. The corpus is the witness; these constraints are
-- what stop the marker from being forged by anyone who cannot already write here.
--
-- WHAT THIS FILE DELIBERATELY DOES NOT DO
-- =======================================
-- * It creates no table. `apps/trust/tests/test_schema_grants.py:EXPECTED_TABLES` enumerates
--   the `app` schema, and a new relation there is another lane's contract to change.
-- * It creates no foreign key, for the same reason (`EXPECTED_FOREIGN_KEYS`).
-- * It contains no `GRANT ... ON SCHEMA ... TO ... exchange`. D5's acceptance scan requires
--   the union of schemas granted to the auction role, across every migration file, to equal
--   exactly {ledger, app}; S7 fails on a third. The new column needs no grant of its own --
--   0004's `GRANT SELECT ON ALL TABLES IN SCHEMA app TO exchange` is a table-level privilege
--   and covers every column the table grows.

-- ---------------------------------------------------------------------------------------
-- Bounded waits, first statement in the file -- 0004's reasoning, and for the same reason:
-- the runner holds every lock this file takes until end-of-file, and an ALTER TABLE takes
-- ACCESS EXCLUSIVE.
-- ---------------------------------------------------------------------------------------
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';

-- The column. `IF NOT EXISTS` and a DEFAULT rather than a backfill: every row that exists
-- before this migration runs was written by `publish_profile` from a served login, so 'live'
-- is the true value for all of them and not merely a convenient one.
ALTER TABLE app.buyer_accounts
  ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'live';

-- `ADD CONSTRAINT` has no `IF NOT EXISTS` in any Postgres version, and this file is applied
-- more than once (`test_migrations_are_idempotent_in_the_same_database` re-applies the whole
-- set into a live database and compares a catalog fingerprint across the re-run). Guarded by
-- catalog lookup rather than by `EXCEPTION WHEN duplicate_object`, so that a constraint which
-- fails to ADD for any OTHER reason -- a row already violating it -- is a loud failure rather
-- than a swallowed one.
DO $provenance$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'buyer_accounts_provenance_vocabulary'
      AND conrelid = 'app.buyer_accounts'::regclass
  ) THEN
    ALTER TABLE app.buyer_accounts
      ADD CONSTRAINT buyer_accounts_provenance_vocabulary
      CHECK (provenance IN ('live', 'seed'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'buyer_accounts_provenance_is_the_key'
      AND conrelid = 'app.buyer_accounts'::regclass
  ) THEN
    -- The whole guarantee, in one expression. `=` between two booleans is `IFF`: the label
    -- and the key agree, or the row does not exist. Written as an equivalence rather than as
    -- two one-way implications because only the equivalence closes BOTH forgeries, and the
    -- one-way form is the shape somebody "simplifies" this into.
    --
    -- The literal 'psn-seed-' is `buyer_svc.vault.PSEUDONYM_PREFIX` + `apps.buyer.seed.chain
    -- .SEED_MARKER`, and `test_the_seed_marker_the_database_enforces_is_the_one_python_mints`
    -- reads this file and asserts the two spellings still agree -- a marker that lived in a
    -- Python constant and an unrelated SQL literal is one rename away from a table where the
    -- constraint passes and the reader classifies every row 'live'.
    ALTER TABLE app.buyer_accounts
      ADD CONSTRAINT buyer_accounts_provenance_is_the_key
      CHECK ((provenance = 'seed') = (pseudonym LIKE 'psn-seed-%'));
  END IF;
END
$provenance$;

-- Whose query this is, stated exactly, because the first version of this comment said "the
-- window's read is `... WHERE provenance = ... ORDER BY pseudonym`" and that is FALSE: the
-- window selects the whole table with no WHERE clause at all
-- (`buyer_svc.window.routes._SELECT`), because a k-anonymity floor has to count every row
-- before it can release any. This index cannot serve that query and is not meant to.
--
-- It serves `python -m apps.buyer.seed audit`, whose read IS
-- `select pseudonym, buckets from app.buyer_accounts where provenance = 'seed'` -- the check
-- that every manufactured buyer in the database is one the repository committed to, which is
-- the one thing the CHECK constraints above cannot establish. Partial rather than plain:
-- 'live' and 'seed' are the only two values the vocabulary admits, so a full index on a
-- two-value column would never be chosen, while an index on the seeded half answers the audit
-- without a sequential scan over a table that is mostly real buyers.
CREATE INDEX IF NOT EXISTS buyer_accounts_seeded_idx
  ON app.buyer_accounts (pseudonym) WHERE provenance = 'seed';
