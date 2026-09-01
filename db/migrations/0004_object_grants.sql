-- 0004 — object-level privileges. Owned by T-011. Runs last, after every table exists.
--
-- CARRY-FORWARD CF-3, in one sentence: schema USAGE is name resolution, not row access.
-- 0001 grants USAGE; without this file the auction role resolves `ledger.commerce_events`
-- and then fails with `permission denied for table ledger.commerce_events`. The two files
-- together are the grant model, and neither half is optional.
--
-- Every statement below re-runs safely: GRANT and REVOKE are idempotent, and
-- `ON ALL TABLES IN SCHEMA` is evaluated at execution time, so re-applying the migration
-- set after a later migration adds a table brings that table's privileges into line. The
-- ALTER DEFAULT PRIVILEGES statements cover the window before that re-run happens.

-- ---------------------------------------------------------------------------------------
-- The read-only auction role. SELECT on the two open schemas, nothing anywhere else, and
-- no write anywhere at all (DESIGN §Data models: "`exchange` role: no write").
-- ---------------------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA ledger, app TO exchange;
ALTER DEFAULT PRIVILEGES IN SCHEMA ledger, app GRANT SELECT ON TABLES TO exchange;

-- Explicit and belt-and-braces: no write on the schemas it CAN read...
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON ALL TABLES IN SCHEMA ledger, app FROM exchange;
ALTER DEFAULT PRIVILEGES IN SCHEMA ledger, app
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLES FROM exchange;

-- ...and nothing whatsoever on the two closed schemas. These are no-ops against a correct
-- 0001 -- their job is to fail loudly in review if anyone ever makes them not be.
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA sealed FROM exchange;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA sealed FROM exchange;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA sealed FROM exchange;
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA vault  FROM exchange;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA vault  FROM exchange;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA vault  FROM exchange;
ALTER DEFAULT PRIVILEGES IN SCHEMA sealed REVOKE ALL ON TABLES    FROM exchange;
ALTER DEFAULT PRIVILEGES IN SCHEMA sealed REVOKE ALL ON SEQUENCES FROM exchange;
ALTER DEFAULT PRIVILEGES IN SCHEMA vault  REVOKE ALL ON TABLES    FROM exchange;
ALTER DEFAULT PRIVILEGES IN SCHEMA vault  REVOKE ALL ON SEQUENCES FROM exchange;

-- ---------------------------------------------------------------------------------------
-- trust_rw -- read/write on the ledger, read on the application schema, and the one write
-- it needs outside its own schema: maintaining the blacklist it decides.
-- ---------------------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ledger TO trust_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ledger TO trust_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO trust_rw;
GRANT INSERT, UPDATE, DELETE ON app.seller_blacklist TO trust_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA ledger
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trust_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA ledger GRANT USAGE, SELECT ON SEQUENCES TO trust_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO trust_rw;

-- ---------------------------------------------------------------------------------------
-- buyer_vault -- the only role that reaches the buyer identity schema.
-- ---------------------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA vault TO buyer_vault;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vault TO buyer_vault;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO buyer_vault;
ALTER DEFAULT PRIVILEGES IN SCHEMA vault
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO buyer_vault;
ALTER DEFAULT PRIVILEGES IN SCHEMA vault GRANT USAGE, SELECT ON SEQUENCES TO buyer_vault;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO buyer_vault;

-- ---------------------------------------------------------------------------------------
-- app -- the general application role. Owns the application schema, is the role behind the
-- merchant / store-agent surfaces that legitimately reach seller strategy, and appends to
-- the ledger. It never reaches the buyer identity schema: 0001 grants it no USAGE there.
-- ---------------------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA sealed TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA sealed TO app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA ledger TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ledger TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT USAGE, SELECT ON SEQUENCES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA sealed
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA sealed GRANT USAGE, SELECT ON SEQUENCES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA ledger GRANT SELECT, INSERT ON TABLES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA ledger GRANT USAGE, SELECT ON SEQUENCES TO app;
