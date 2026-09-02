-- 0001 — schemas, idempotent role creation, and the per-database grant model.
-- Owned by T-011. Applied by apps/trust/src/ledger/migrations.py (and by hand with psql -f).
--
-- D39 — roles are CLUSTER-GLOBAL (pg_authid.relisshared = true), grants are PER-DATABASE.
-- db/init/00-roles.sql (T-000, frozen) creates the four roles once, on a fresh volume, via
-- the postgres image's docker-entrypoint-initdb.d hook. That hook does NOT run for a
-- database created later from a template, and it does not run at all on an existing volume,
-- so this file re-creates the roles idempotently -- guarded so six workers applying these
-- migrations concurrently cannot race on the shared catalog -- and then issues the GRANTs,
-- which every proxyshop_w<n> needs its own copy of.
--
-- D5, AND THE CORRECTION TO ITS WORDING (carry-forward CF-3). D5's summary sentence and
-- db/init/00-roles.sql:11 both say the model is "schema-level USAGE only". Taken literally
-- that is wrong and it does not work: USAGE on a schema grants NAME RESOLUTION, not row
-- access, so `GRANT USAGE ON SCHEMA ledger TO exchange` alone leaves every read failing
-- with `permission denied for table ledger.<t>`. The half of D5 that is exact is the
-- NEGATIVE half, and that is what release blocker S7 turns on:
--
--     exchange     reads ledger.* and app.*        -- USAGE on the schema PLUS SELECT on
--                                                     its tables. No write, anywhere.
--                  NOTHING on the two closed schemas. Not USAGE, not SELECT, not a
--                  default privilege, not a role membership that could inherit one.
--     trust_rw     read/write on the ledger schema; reads app; maintains the blacklist.
--     buyer_vault  the ONLY role with access to the buyer identity schema.
--     app          the general application role: owns app.*, and is the role behind the
--                  merchant/store-agent surfaces that legitimately reach the closed
--                  seller-strategy schema. Never reaches the buyer identity schema.
--
-- Reading order note for a later maintainer: the two closed schemas are deliberately never
-- named on the same line as this role, so a careless copy-paste of a GRANT cannot pick one
-- up by accident. The revokes at the bottom of this file are the belt to that braces.

-- ---------------------------------------------------------------------------------------
-- Bounded waits, first statement in the file.
--
-- The runner executes each migration inside ONE transaction and holds every lock it takes
-- until end-of-file, so one blocked statement stalls the whole file -- and with no
-- `lock_timeout` set anywhere in this repo, "stalls" meant FOREVER rather than for a bounded
-- interval. That is the shape of failure this project has already lost real time to, so it
-- is bounded here rather than diagnosed again. `SET LOCAL` scopes both settings to this
-- file's transaction and restores whatever the session had on COMMIT, so a migration can
-- never leave a timeout behind on a pooled connection. Applying a file by hand: wrap it in
-- BEGIN/COMMIT, or psql warns that SET LOCAL outside a transaction block does nothing.
-- ---------------------------------------------------------------------------------------
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';

CREATE SCHEMA IF NOT EXISTS ledger;
CREATE SCHEMA IF NOT EXISTS sealed;
CREATE SCHEMA IF NOT EXISTS vault;
CREATE SCHEMA IF NOT EXISTS app;

-- D39: idempotent, race-safe role creation. `IF NOT EXISTS` around CREATE ROLE is not
-- enough on its own -- two workers can both pass the existence check and one then fails --
-- so the duplicate is caught as well.
--
-- TWO SQLSTATEs, not one, and the second is the one that matters. `duplicate_object`
-- (42710) is what CREATE ROLE raises from its own PRE-INSERT catalog lookup -- i.e. the
-- SEQUENTIAL re-run case, where the role plainly already exists and no guard was needed.
-- In the genuinely CONCURRENT case the loser does not reach that lookup at all: it blocks
-- on the unique index `pg_authid_rolname_index`, waits for the winner to commit, and then
-- raises `unique_violation` (23505). Catching only 42710 therefore missed the exact race
-- these lines were written for. `pg_authid` is a SHARED catalog, so this race is across
-- every worker database on the cluster at once; a per-database advisory lock (which the
-- runner takes for the rest of the file) cannot serialise it, and the SQLSTATE catch is
-- what does.
--
-- The password is dev-only and sourced from `proxyshop.role_password`, which the runner
-- sets from $PROXYSHOP_ROLE_PASSWORD; the historical literal 'x' remains the default so a
-- checkout with no environment behaves exactly as before. It applies at CREATE only --
-- these roles are cluster-global and shared by every worker, so a migration must never
-- reset a password another worker's live connections are using.
DO $roles$
DECLARE
  role_name text;
  role_password text := coalesce(
    nullif(current_setting('proxyshop.role_password', true), ''), 'x'
  );
BEGIN
  FOREACH role_name IN ARRAY ARRAY['exchange', 'trust_rw', 'buyer_vault', 'app'] LOOP
    BEGIN
      EXECUTE format(
        'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT',
        role_name, role_password
      );
    EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
    END;
  END LOOP;
END
$roles$;

-- The migration runner's bookkeeping table. It lives in `ledger` because that schema is
-- created above, in this same file, before anything else needs it.
CREATE TABLE IF NOT EXISTS ledger.schema_migrations (
  filename    text        PRIMARY KEY,
  checksum    text        NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

-- No role may create objects in `public`; every schema is granted explicitly below. This
-- repeats db/init/00-roles.sql on purpose: a database created from `template1` rather than
-- from `proxyshop_template` never inherited that revoke.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ---------------------------------------------------------------------------------------
-- Schema-level USAGE. The frozen acceptance scan reads exactly these statements: the union
-- of schemas granted via `GRANT ... ON SCHEMA <s[,s]> TO ...exchange...` must equal exactly
-- {ledger, app}. Adding a third schema here, in any migration file, fails S7.
-- ---------------------------------------------------------------------------------------
GRANT USAGE ON SCHEMA ledger, app TO exchange;
GRANT USAGE ON SCHEMA ledger, app TO trust_rw;
GRANT USAGE ON SCHEMA app TO buyer_vault;
GRANT USAGE ON SCHEMA app, ledger TO app;

-- The closed schemas, granted only to the roles that own them. Neither statement names the
-- read-only auction role, and neither ever may.
GRANT USAGE ON SCHEMA sealed TO app;
GRANT USAGE ON SCHEMA vault TO buyer_vault;

-- ---------------------------------------------------------------------------------------
-- Defense in depth. Nothing above grants the read-only auction role anything on the two
-- closed schemas, so these revokes are no-ops today. They exist so that a future migration
-- which grants something to PUBLIC, or a template database that arrived with an
-- inherited privilege, cannot open the hole silently.
-- ---------------------------------------------------------------------------------------
REVOKE ALL PRIVILEGES ON SCHEMA sealed FROM exchange;
REVOKE ALL PRIVILEGES ON SCHEMA vault FROM exchange;
REVOKE ALL PRIVILEGES ON SCHEMA sealed FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA vault FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA ledger FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA app FROM PUBLIC;
