-- 0004 — object-level privileges. Owned by T-011. Runs last, after every table exists.
--
-- CARRY-FORWARD CF-3, in one sentence: schema USAGE is name resolution, not row access.
-- 0001 grants USAGE; without this file the auction role resolves `ledger.commerce_events`
-- and then fails with `permission denied for table ledger.commerce_events`. The two files
-- together are the grant model, and neither half is optional.
--
-- Every statement below re-runs safely: GRANT and REVOKE are idempotent, and
-- `ON ALL TABLES IN SCHEMA` is evaluated at execution time, so re-applying the migration
-- set after a later migration adds a table brings that table's privileges into line.
--
-- ON `FOR ROLE`. Default privileges are recorded PER GRANTOR: `ALTER DEFAULT PRIVILEGES`
-- with no `FOR ROLE` governs only objects created by the role running the statement. That
-- made the defaults cover the one case never at risk -- the migration runner, whose new
-- tables the `ON ALL TABLES` re-run already fixes -- and cover nothing at all for any other
-- creator. Every statement below therefore names its grantors explicitly. Today no role
-- holds CREATE on any of these schemas (0001 revokes it from PUBLIC and grants it to
-- nobody), so the extra grantors are dormant; the point is that they stop being a hole the
-- moment that changes.
--
-- One deliberate omission: the exchange-facing statement below lists CURRENT_USER, trust_rw
-- and app, but NOT the buyer identity role. Two reasons, and both are real. That role
-- creates nothing in `ledger` or `app`, so it would be dormant anyway -- and its NAME
-- contains the substring the S7 static scan searches for, so naming it in a statement that
-- also grants to the auction role would read, correctly by that scan's rules, as a grant
-- reaching the closed schema.

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

-- ---------------------------------------------------------------------------------------
-- The read-only auction role. SELECT on the two open schemas, nothing anywhere else, and
-- no write anywhere at all (DESIGN §Data models: "`exchange` role: no write").
-- ---------------------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA ledger, app TO exchange;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app IN SCHEMA ledger, app
  GRANT SELECT ON TABLES TO exchange;

-- Explicit and belt-and-braces: no write on the schemas it CAN read...
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON ALL TABLES IN SCHEMA ledger, app FROM exchange;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA ledger, app
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLES FROM exchange;

-- ...and nothing whatsoever on the two closed schemas. These are no-ops against a correct
-- 0001 -- their job is to fail loudly in review if anyone ever makes them not be.
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA sealed FROM exchange;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA sealed FROM exchange;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA sealed FROM exchange;
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA vault  FROM exchange;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA vault  FROM exchange;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA vault  FROM exchange;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA sealed REVOKE ALL ON TABLES FROM exchange;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA sealed REVOKE ALL ON SEQUENCES FROM exchange;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA vault REVOKE ALL ON TABLES FROM exchange;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA vault REVOKE ALL ON SEQUENCES FROM exchange;

-- ---------------------------------------------------------------------------------------
-- The PUBLIC hole, closed and kept closed.
--
-- Every revoke above names a role, and the S7 static scan only reads statements that name
-- the auction role -- so `GRANT SELECT ON ALL TABLES IN SCHEMA sealed TO PUBLIC` was a way
-- to hand it the closed schemas that no scan would see, and that re-applying this migration
-- set did not heal, because 0001 revoked only SCHEMA privileges from PUBLIC. These do the
-- object level, so a correct re-run repairs the database rather than merely not making it
-- worse. Live denial tests are what catch the breach; this is what fixes it.
-- ---------------------------------------------------------------------------------------
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA sealed, vault FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA sealed, vault FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA sealed, vault FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA ledger, app FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA ledger, app FROM PUBLIC;

-- ---------------------------------------------------------------------------------------
-- The chain's mutual-exclusion primitive is not public.
--
-- CHAIN_LOCK_KEY is a published constant derived from the table name, `pg_advisory_lock` is
-- EXECUTE-to-PUBLIC by default, and an advisory lock is cooperative -- so ANY role that can
-- connect, including the read-only auction role that holds no write anywhere, could take
-- the ledger's lock and sit on it, blocking every append in the database. A role documented
-- "no write" must not hold the writers' mutex. Function ACLs are per-database, so this is a
-- per-database statement like every other grant here.
--
-- ALL EIGHT bigint overloads, not four. Postgres has ONE 8-byte advisory-lock space, and
-- every one of these functions takes a lock in it: the four exclusive forms below and the
-- four `_shared` forms beside them. `ShareLock` conflicts with `ExclusiveLock`, so a role
-- holding `pg_advisory_lock_shared(776167449)` blocks `pg_advisory_xact_lock(776167449)`
-- exactly as an exclusive holder would -- and with no `lock_timeout` the blocked append
-- waits forever, not for a bounded interval. Revoking only the exclusive half left the
-- read-only auction role able to halt every ledger append in the database with one SELECT
-- against a published constant. Proven live before this line was written:
--   exchange pg_advisory_lock_shared: ALLOWED; app's append while it was held: BLOCKED.
-- The two-argument (int, int) overloads use the OTHER, disjoint key space and cannot
-- conflict with CHAIN_LOCK_KEY, so they are deliberately not named here.
-- ---------------------------------------------------------------------------------------
REVOKE EXECUTE ON FUNCTION pg_advisory_xact_lock(bigint)            FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_advisory_lock(bigint)                 FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_try_advisory_xact_lock(bigint)        FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_try_advisory_lock(bigint)             FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_advisory_xact_lock_shared(bigint)     FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_advisory_lock_shared(bigint)          FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_try_advisory_xact_lock_shared(bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_try_advisory_lock_shared(bigint)      FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg_advisory_xact_lock(bigint)            TO trust_rw, app;
GRANT EXECUTE ON FUNCTION pg_advisory_lock(bigint)                 TO trust_rw, app;
GRANT EXECUTE ON FUNCTION pg_try_advisory_xact_lock(bigint)        TO trust_rw, app;
GRANT EXECUTE ON FUNCTION pg_try_advisory_lock(bigint)             TO trust_rw, app;
GRANT EXECUTE ON FUNCTION pg_advisory_xact_lock_shared(bigint)     TO trust_rw, app;
GRANT EXECUTE ON FUNCTION pg_advisory_lock_shared(bigint)          TO trust_rw, app;
GRANT EXECUTE ON FUNCTION pg_try_advisory_xact_lock_shared(bigint) TO trust_rw, app;
GRANT EXECUTE ON FUNCTION pg_try_advisory_lock_shared(bigint)      TO trust_rw, app;


-- ---------------------------------------------------------------------------------------
-- trust_rw -- read/write on the ledger, read on the application schema, and the one write
-- it needs outside its own schema: maintaining the blacklist it decides.
-- ---------------------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ledger TO trust_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ledger TO trust_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO trust_rw;
GRANT INSERT, UPDATE, DELETE ON app.seller_blacklist TO trust_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA ledger GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trust_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA ledger GRANT USAGE, SELECT ON SEQUENCES TO trust_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA app GRANT SELECT ON TABLES TO trust_rw;

-- ---------------------------------------------------------------------------------------
-- buyer_vault -- the only role that reaches the buyer identity schema.
-- ---------------------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA vault TO buyer_vault;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vault TO buyer_vault;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO buyer_vault;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA vault GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO buyer_vault;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA vault GRANT USAGE, SELECT ON SEQUENCES TO buyer_vault;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA app GRANT SELECT ON TABLES TO buyer_vault;

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
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA app GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA app GRANT USAGE, SELECT ON SEQUENCES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA sealed GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA sealed GRANT USAGE, SELECT ON SEQUENCES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA ledger GRANT SELECT, INSERT ON TABLES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER, trust_rw, app, buyer_vault
  IN SCHEMA ledger GRANT USAGE, SELECT ON SEQUENCES TO app;

-- ---------------------------------------------------------------------------------------
-- The migration audit record is not rewritable by the roles it audits. `schema_migrations`
-- lives in `ledger`, so the blanket ledger grants below would otherwise let the trust
-- writer edit the record of which migrations ran -- and a test reads that table as evidence.
-- ---------------------------------------------------------------------------------------
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ledger.schema_migrations FROM trust_rw, app;

-- The anchor row moves on every append, and the AFTER INSERT trigger that moves it runs as
-- the INSERTing role (it is not SECURITY DEFINER, deliberately -- there is no such function
-- anywhere in this schema). So the two roles that may append need UPDATE on that ONE row's
-- table. The read-only auction role keeps SELECT on it, which is what lets it check the
-- ledger it can read.
--
-- UPDATE ONLY, and INSERT/DELETE explicitly taken back. `chain_head` is the ONLY mechanism
-- that can detect tail truncation, so it is the one table in this schema whose writers must
-- not also be able to re-author it. The blanket `SELECT, INSERT, UPDATE, DELETE ON ALL
-- TABLES IN SCHEMA ledger TO trust_rw` above reaches it, and `SELECT, INSERT ... TO app`
-- reaches it too, and the earlier wording of this comment -- "nothing else gains a write it
-- did not already have" -- was wrong: the append path grants chain-GUARDED, append-only
-- writes, while INSERT and DELETE here grant ARBITRARY control of the commitment. Proven
-- live before these lines were written, as trust_rw:
--   DELETE FROM ledger.chain_head           -> ALLOWED; appends then kept succeeding with
--                                              no anchor at all and no error;
--   re-INSERT a forged anchor               -> a truncated chain verified {'ok': True}.
-- The trigger added by 0002 is the second half of this: grants stop the two roles, and the
-- trigger stops anyone who reaches the table by some other route.
GRANT UPDATE ON ledger.chain_head TO trust_rw, app;
REVOKE INSERT, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ledger.chain_head FROM trust_rw, app;
