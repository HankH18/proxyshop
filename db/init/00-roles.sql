-- Cluster-global role creation. Orchestrator-owned (T-000); the password plumbing is T-110.
--
-- Why this file exists at all (D38): Postgres roles are cluster-global
-- (pg_authid.relisshared = true), so they cannot live in db/migrations, which runs once per
-- per-worker database (proxyshop_w1 .. proxyshop_w6). Creating them there would make six
-- workers race on the same shared catalog. They are created ONCE here instead, by the
-- postgres image's docker-entrypoint-initdb.d hook on a fresh volume.
--
-- What is deliberately NOT here (D5/D38): GRANTs. Privileges are per-database, so every
-- worker database needs its own copy and they belong in db/migrations (T-011). The grant
-- model those migrations must reproduce — verified live, D5 — is schema-level USAGE only:
--
--     exchange     USAGE on schema `ledger` and schema `app`.
--                  NEVER on `sealed`, NEVER on `vault`, and no write grant anywhere (C3/S7).
--     trust_rw     read/write on the trust ledger schema.
--     buyer_vault  the only role with access to `vault`.
--     app          the general application role.
--
-- Idempotent on purpose: `docker compose up` on an existing volume skips initdb entirely,
-- but a human re-running this file by hand (psql -f) must not error.
--
-- ---------------------------------------------------------------------------------------
-- THE PASSWORD HAS ONE SOURCE OF TRUTH (T-110)
-- ---------------------------------------------------------------------------------------
-- It is dev-only, it is read from $PROXYSHOP_ROLE_PASSWORD, and the historical literal 'x'
-- is the documented default so a checkout with no environment behaves exactly as it always
-- has. That is the same environment variable, through the same custom GUC, with the same
-- default expression that db/migrations/0001_schemas_roles_grants.sql uses -- see
-- ROLE_PASSWORD_ENV / ROLE_PASSWORD_SETTING in apps/trust/src/ledger/migrations.py -- so
-- the two halves of D39's split schema can no longer disagree about the credential. They
-- previously did, and this file won: its else-branch reset all four roles to the literal
-- 'x' unconditionally, so setting the environment variable changed nothing on a fresh
-- volume and produced two sources of truth for one password.
--
-- The password applies at CREATE ONLY. These roles are cluster-global and shared by every
-- worker database, so re-running this file must never reset a password that another
-- worker's live connections are authenticating with; the else-branch below normalises the
-- role ATTRIBUTES and deliberately leaves the password alone. That is the same rule
-- migrations.py states for 0001, for the same reason.
--
-- Requires psql: `\getenv` is a psql metacommand (psql 14 or newer; the image is
-- postgres:16-alpine). The initdb hook runs .sql files through psql, and `psql -f` is the
-- documented by-hand path, so both callers have it.
--
-- WIRING (T-112, and the note this replaces). This file runs INSIDE the postgres container,
-- so it can only read what the container has. T-110 left docker-compose.yml forwarding
-- nothing, which meant `make deps-up` got the 'x' default no matter what the host
-- environment said -- T-110's own defect, one layer down, on the only volume the project
-- actually creates. docker-compose.yml's postgres service now forwards
-- PROXYSHOP_ROLE_PASSWORD: "${PROXYSHOP_ROLE_PASSWORD:-}", with a deliberately EMPTY
-- compose-side default so the coalesce below stays the single home of the literal. The
-- CONNECT side reads the same variable in proxyshop_support/postgres.py (role_dsn), so a
-- seeded cluster is also a reachable one; proxyshop_support/tests/test_role_password_
-- end_to_end.py proves both halves against a real fresh volume.
--
-- The hook runs ONCE, when the pgdata volume is created: setting the variable after that
-- changes nothing on an existing volume, by design (these roles are cluster-global and
-- shared by every live worker).

-- `\set` first so an ABSENT variable is empty rather than undefined: `\getenv` leaves its
-- target untouched when the environment variable does not exist, and an undefined psql
-- variable would interpolate as literal text below rather than as a value.
\set proxyshop_role_password ''
\getenv proxyshop_role_password PROXYSHOP_ROLE_PASSWORD
-- `SET`, not `SELECT set_config(...)`: it hands the value to the server without echoing it
-- into the container's init log. `:'...'` is psql's quoting form, so a password containing
-- a quote is escaped rather than injected.
SET proxyshop.role_password = :'proxyshop_role_password';

DO $roles$
DECLARE
  role_name text;
  -- The only password literal in this file: the documented dev default, applied when the
  -- environment says nothing. Identical expression to db/migrations/0001.
  role_password text := coalesce(
    nullif(current_setting('proxyshop.role_password', true), ''), 'x'
  );
BEGIN
  FOREACH role_name IN ARRAY ARRAY['exchange', 'trust_rw', 'buyer_vault', 'app'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
      EXECUTE format(
        'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT',
        role_name, role_password
      );
    ELSE
      -- Attributes only. Never PASSWORD: see the block comment above.
      EXECUTE format(
        'ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT', role_name
      );
    END IF;
  END LOOP;
END
$roles$;

-- No role may create objects in `public` of any database it can connect to; every schema a
-- role needs is granted explicitly by the migrations.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
