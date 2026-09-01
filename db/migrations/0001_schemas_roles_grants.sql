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

CREATE SCHEMA IF NOT EXISTS ledger;
CREATE SCHEMA IF NOT EXISTS sealed;
CREATE SCHEMA IF NOT EXISTS vault;
CREATE SCHEMA IF NOT EXISTS app;

-- D39: idempotent, race-safe role creation. `IF NOT EXISTS` around CREATE ROLE is not
-- enough on its own -- two workers can both pass the existence check and one then fails --
-- so the duplicate is caught as well.
DO $$
BEGIN
  BEGIN
    CREATE ROLE exchange LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE ROLE trust_rw LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE ROLE buyer_vault LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
  BEGIN
    CREATE ROLE app LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  EXCEPTION WHEN duplicate_object THEN NULL;
  END;
END
$$;

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
