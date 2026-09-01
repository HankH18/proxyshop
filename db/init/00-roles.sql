-- Cluster-global role creation. Orchestrator-owned (T-000), frozen.
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
-- but a human re-running this file by hand (psql -f) must not error. Passwords are the
-- development placeholders from .env.example; nothing here is a real credential.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'exchange') THEN
    CREATE ROLE exchange LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  ELSE
    ALTER ROLE exchange LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trust_rw') THEN
    CREATE ROLE trust_rw LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  ELSE
    ALTER ROLE trust_rw LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'buyer_vault') THEN
    CREATE ROLE buyer_vault LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  ELSE
    ALTER ROLE buyer_vault LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') THEN
    CREATE ROLE app LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  ELSE
    ALTER ROLE app LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  END IF;
END
$$;

-- No role may create objects in `public` of any database it can connect to; every schema a
-- role needs is granted explicitly by the migrations.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
