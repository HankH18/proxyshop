-- 0003 — the three non-ledger schemas (DESIGN §Data models).
-- Owned by T-011.
--
--   sealed.*  seller strategy. Envelopes (versioned), learned policy, interview
--             transcripts, shadow bids. The auction role holds NOTHING here (S7) -- that
--             is enforced by the absence of any grant, in 0001 and 0004.
--   vault.*   buyer identity: the email <-> pseudonym history. One role reaches it.
--   app.*     everything the application layer needs that is neither strategy nor identity.
--
-- Amendment 1 / D52 lands two of the app.* tables, and they are the two the frozen
-- acceptance suite cannot grade -- `bid_nonces`, `seller_endpoints` and `key_id` appear in
-- zero files under .swarm-loop/acceptance/. They are built because T-044's signed external
-- door has no durable backing without them, and because the keying is the whole point:
--
--   * seller_endpoints is one row PER LIVE KEY. `key_id` is unique only WITHIN a
--     `signer_id`; two different signers may legitimately use the same `key_id` string.
--     A schema with one key per store, or a primary key on `key_id` alone, is wrong.
--   * bid_nonces is UNIQUE on `(signer_id, nonce)` -- per signer, never global -- and
--     `retain_until` is set PAST the auction's `respond_by`, because holding the nonce in
--     the Redis auction state instead would reopen same-payload temporal replay the moment
--     that state expires.

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
-- sealed.* -- seller strategy. Never legible to the auction.
-- ---------------------------------------------------------------------------------------
-- T-350. The five `approval_*` columns and the two constraints beside them are not
-- bookkeeping: they are the storage half of R6. `activation` is a plain text column, and
-- before they existed a row could assert 'active' with nothing recording WHO approved it,
-- WHEN, or WHAT they approved -- the exact state
-- `merchant_svc.envelope.store._refuse_unapproved_activation` refuses in memory (T-248). A
-- rule the table does not share is one bug away from being no rule, and the durability seam
-- paid for it: `merchant_svc.envelope.repository.restorable` had to downgrade EVERY row read
-- back as 'active' to shadow, because the artifact that would justify it was not storable, so
-- an approved envelope silently stopped bidding across a restart.
--
-- The columns mirror `merchant_svc.envelope.model.ApprovalArtifact` field for field:
-- approver / approved_at / envelope_hash are the three the artifact requires, note and
-- document_ref are the two it makes optional. `approval_envelope_hash` is what makes an
-- approval BOUND -- it is `approval_digest()` over the terms the merchant read -- so an
-- approval lifted onto another version's terms stops verifying. The digest cannot be checked
-- here (it is a hash over seven columns' canonical rendering); the table enforces the half it
-- can, which is that the record EXISTS and is whole, and the domain re-checks the binding on
-- every load.
--
-- Deliberately NOT constrained: a 'killed' row may keep its approval. `kill_envelope` leaves
-- the artifact attached on purpose -- it is the record of what the store WAS running -- so a
-- symmetric "only active rows carry an approval" constraint would refuse an honest kill.
CREATE TABLE IF NOT EXISTS sealed.envelopes (
  store_id                text        NOT NULL,
  version                 integer     NOT NULL,
  activation              text        NOT NULL,
  max_discount_pct        numeric(6, 3),
  budget_cap              numeric(14, 2),
  floors                  jsonb       NOT NULL DEFAULT '[]'::jsonb,
  pursue_clusters         jsonb       NOT NULL DEFAULT '[]'::jsonb,
  standing_commitments    jsonb       NOT NULL DEFAULT '[]'::jsonb,
  created_at              timestamptz NOT NULL DEFAULT now(),
  -- After `created_at`, not beside the other terms, so a database CREATED by this file and a
  -- database ALTERED into shape by the block below have the SAME column order: `ADD COLUMN`
  -- can only append. Nothing here reads a row by ordinal -- every statement in
  -- `merchant_svc.envelope.repository` names its columns -- but `select *` and `\d` are what
  -- a human compares two databases with, and two shapes that differ only in column order are
  -- a false positive nobody needs to chase.
  approval_approver       text,
  approval_approved_at    timestamptz,
  approval_envelope_hash  text,
  approval_note           text,
  approval_document_ref   text,
  CONSTRAINT envelopes_pkey PRIMARY KEY (store_id, version),
  CONSTRAINT envelopes_version_positive   CHECK (version >= 1),
  CONSTRAINT envelopes_activation_check   CHECK (activation IN ('shadow', 'active', 'killed')),
  CONSTRAINT envelopes_discount_range     CHECK (
    max_discount_pct IS NULL OR (max_discount_pct >= 0 AND max_discount_pct <= 100)
  ),
  CONSTRAINT envelopes_floors_is_array    CHECK (jsonb_typeof(floors) = 'array'),
  -- An approval is present or it is absent; there is no half of one. The blank-string arms
  -- are not decoration: `text NOT NULL` is satisfied by '', and an approval signed by ''
  -- names no approver -- which is precisely what `ApprovalArtifact.parse` refuses.
  CONSTRAINT envelopes_approval_is_whole  CHECK (
    (approval_approver IS NULL) = (approval_approved_at IS NULL)
    AND (approval_approver IS NULL) = (approval_envelope_hash IS NULL)
    AND (approval_approver IS NOT NULL
         OR (approval_note IS NULL AND approval_document_ref IS NULL))
    AND (approval_approver IS NULL OR btrim(approval_approver) <> '')
    AND (approval_envelope_hash IS NULL OR btrim(approval_envelope_hash) <> '')
  ),
  -- The constraint the ticket is about. 'active' stays a legal value; it stops being a value
  -- a row may assert on its own say-so.
  CONSTRAINT envelopes_active_requires_approval CHECK (
    activation <> 'active'
    OR (approval_approver IS NOT NULL
        AND approval_approved_at IS NOT NULL
        AND approval_envelope_hash IS NOT NULL)
  )
);

-- Idempotent repair for a database created by an earlier run of this file, guarded exactly
-- as the `app.seller_endpoints` and `app.bid_nonces` blocks below are, and for the same
-- reason: `CREATE TABLE IF NOT EXISTS` does nothing at all to a table that already exists, so
-- a live database would otherwise keep the old shape forever while this file claims the new
-- one. Adding a nullable column with no default is a catalog change in PostgreSQL 11+ -- no
-- table rewrite -- and the file's `lock_timeout` bounds the ACCESS EXCLUSIVE it still takes.
ALTER TABLE sealed.envelopes ADD COLUMN IF NOT EXISTS approval_approver      text;
ALTER TABLE sealed.envelopes ADD COLUMN IF NOT EXISTS approval_approved_at   timestamptz;
ALTER TABLE sealed.envelopes ADD COLUMN IF NOT EXISTS approval_envelope_hash text;
ALTER TABLE sealed.envelopes ADD COLUMN IF NOT EXISTS approval_note          text;
ALTER TABLE sealed.envelopes ADD COLUMN IF NOT EXISTS approval_document_ref  text;

-- WHAT HAPPENS TO ROWS THAT ARE ALREADY THERE, stated rather than discovered.
--
-- Every row that predates this change has all five approval columns NULL, so any row already
-- asserting 'active' violates `envelopes_active_requires_approval`. A migration that simply
-- added the constraint would abort on the first such row -- and because the runner wraps this
-- file in ONE transaction, it would abort the whole file, on every subsequent run, until an
-- operator went in by hand. So the rows are repaired first, and the repair is not a judgement
-- call: it is the SAME decision the service has been making on every boot since the
-- durability seam landed. `merchant_svc.envelope.repository.restorable` reads a row back as
-- 'active' with no artifact and restores it in 'shadow' at WARNING. This statement makes that
-- downgrade durable and does it once, instead of the service re-deciding it forever.
--
-- The direction is the safe one and it is the only one available: a store stops bidding, and
-- a restart can never START one. The merchant re-activates against a fresh approval, which is
-- the same thing they already had to do. Nothing is deleted -- the terms, the version and the
-- history are untouched; only the lifecycle flag moves, and only for rows whose activation
-- was never justified by anything on file.
UPDATE sealed.envelopes
   SET activation = 'shadow'
 WHERE activation = 'active'
   AND (approval_approver IS NULL
        OR approval_approved_at IS NULL
        OR approval_envelope_hash IS NULL);

DO $envelope_approval_constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
      JOIN pg_class t      ON t.oid = c.conrelid
      JOIN pg_namespace n  ON n.oid = t.relnamespace
     WHERE n.nspname = 'sealed' AND t.relname = 'envelopes'
       AND c.conname = 'envelopes_approval_is_whole'
  ) THEN
    ALTER TABLE sealed.envelopes
      ADD CONSTRAINT envelopes_approval_is_whole
      CHECK (
        (approval_approver IS NULL) = (approval_approved_at IS NULL)
        AND (approval_approver IS NULL) = (approval_envelope_hash IS NULL)
        AND (approval_approver IS NOT NULL
             OR (approval_note IS NULL AND approval_document_ref IS NULL))
        AND (approval_approver IS NULL OR btrim(approval_approver) <> '')
        AND (approval_envelope_hash IS NULL OR btrim(approval_envelope_hash) <> '')
      ) NOT VALID;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
      JOIN pg_class t      ON t.oid = c.conrelid
      JOIN pg_namespace n  ON n.oid = t.relnamespace
     WHERE n.nspname = 'sealed' AND t.relname = 'envelopes'
       AND c.conname = 'envelopes_active_requires_approval'
  ) THEN
    ALTER TABLE sealed.envelopes
      ADD CONSTRAINT envelopes_active_requires_approval
      CHECK (
        activation <> 'active'
        OR (approval_approver IS NOT NULL
            AND approval_approved_at IS NOT NULL
            AND approval_envelope_hash IS NOT NULL)
      ) NOT VALID;
  END IF;
  -- Added NOT VALID above so the ADD itself takes no scan, then validated in a statement of
  -- its own that needs only SHARE UPDATE EXCLUSIVE. A database that already carries both
  -- constraints validated -- every fresh one, since the CREATE TABLE above declares them --
  -- does none of this.
  IF EXISTS (
    SELECT 1 FROM pg_constraint c
      JOIN pg_class t      ON t.oid = c.conrelid
      JOIN pg_namespace n  ON n.oid = t.relnamespace
     WHERE n.nspname = 'sealed' AND t.relname = 'envelopes'
       AND c.conname = 'envelopes_approval_is_whole' AND NOT c.convalidated
  ) THEN
    ALTER TABLE sealed.envelopes VALIDATE CONSTRAINT envelopes_approval_is_whole;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_constraint c
      JOIN pg_class t      ON t.oid = c.conrelid
      JOIN pg_namespace n  ON n.oid = t.relnamespace
     WHERE n.nspname = 'sealed' AND t.relname = 'envelopes'
       AND c.conname = 'envelopes_active_requires_approval' AND NOT c.convalidated
  ) THEN
    ALTER TABLE sealed.envelopes VALIDATE CONSTRAINT envelopes_active_requires_approval;
  END IF;
END
$envelope_approval_constraints$;

CREATE TABLE IF NOT EXISTS sealed.learned_policy (
  policy_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  store_id       text        NOT NULL,
  cluster_id     text        NOT NULL,
  policy_version text        NOT NULL,
  parameters     jsonb       NOT NULL DEFAULT '{}'::jsonb,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT learned_policy_unique UNIQUE (store_id, cluster_id, policy_version)
);

CREATE TABLE IF NOT EXISTS sealed.interview_transcripts (
  transcript_id uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  store_id      text        NOT NULL,
  transcript    jsonb       NOT NULL DEFAULT '[]'::jsonb,
  started_at    timestamptz NOT NULL,
  completed_at  timestamptz
);

CREATE TABLE IF NOT EXISTS sealed.shadow_bids (
  shadow_bid_id uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  auction_id    text        NOT NULL,
  store_id      text        NOT NULL,
  bid           jsonb       NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS shadow_bids_auction_idx ON sealed.shadow_bids (auction_id);

-- ---------------------------------------------------------------------------------------
-- vault.* -- buyer identity. One role, and no other.
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vault.pseudonym_history (
  history_id text        PRIMARY KEY,
  email      text        NOT NULL,
  pseudonym  text        NOT NULL,
  issued_at  timestamptz NOT NULL,
  retired_at timestamptz,
  CONSTRAINT pseudonym_history_pseudonym_key UNIQUE (pseudonym),
  CONSTRAINT pseudonym_history_window_ordered CHECK (retired_at IS NULL OR retired_at >= issued_at)
);

CREATE INDEX IF NOT EXISTS pseudonym_history_email_idx ON vault.pseudonym_history (email);

-- The root conftest.py documents `select * from vault.payment_methods` as the canonical
-- example of a denied read (conftest.py:201-204). The table exists so that example is a
-- real statement against a real relation rather than a syntax error that happens to raise
-- the same class of exception.
CREATE TABLE IF NOT EXISTS vault.payment_methods (
  payment_method_id text        PRIMARY KEY,
  pseudonym         text        NOT NULL REFERENCES vault.pseudonym_history (pseudonym)
                                  ON DELETE RESTRICT,
  provider          text        NOT NULL,
  provider_token    text        NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------
-- app.* -- sellers, keys, nonces, blacklist, and the buyer-facing working set.
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.sellers (
  store_id          text        PRIMARY KEY,
  domain            text        NOT NULL,
  business_identity text        NOT NULL,
  tier              text        NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT sellers_tier_check CHECK (tier IN ('hosted', 'external'))
);

CREATE INDEX IF NOT EXISTS sellers_business_identity_idx ON app.sellers (business_identity);

-- D52: ONE ROW PER LIVE KEY. The primary key is (signer_id, key_id), never key_id alone:
-- rotation means a signer holds more than one live key, and two different signers may use
-- the same key_id string. `signer_id` is the registered submitting identity -- equal to
-- store_id for a single-store seller, distinct when one seller submits for several -- so
-- it is a column of its own and not a synonym for the store.
CREATE TABLE IF NOT EXISTS app.seller_endpoints (
  signer_id  text        NOT NULL,
  key_id     text        NOT NULL,
  store_id   text        NOT NULL REFERENCES app.sellers (store_id) ON DELETE RESTRICT,
  public_key text        NOT NULL,
  status     text        NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  retired_at timestamptz,
  CONSTRAINT seller_endpoints_pkey PRIMARY KEY (signer_id, key_id),
  CONSTRAINT seller_endpoints_status_check CHECK (status IN ('active', 'retired', 'revoked')),
  -- Symmetric on purpose. The one-directional form allowed `status = 'active'` alongside a
  -- past `retired_at`, so the keyring's "is this key live?" question had two answers that
  -- could disagree -- and the exchange boundary would have believed whichever it read.
  CONSTRAINT seller_endpoints_retirement_matches_status CHECK (
    (status = 'active') = (retired_at IS NULL)
  )
);

-- Idempotent repair for a database created by an earlier run of this file.
--
-- The old form of this block was DROP CONSTRAINT IF EXISTS + a bare ADD CONSTRAINT ...
-- CHECK, unconditionally, on EVERY run. `ADD CONSTRAINT ... CHECK` without `NOT VALID`
-- validates immediately: a full sequential scan of the table under ACCESS EXCLUSIVE, held
-- (because the runner wraps the file in one transaction) until end-of-file. Re-running the
-- migrations therefore re-locked and re-scanned a table that had not changed. Now: the
-- legacy name is still dropped (it is a catalog lookup, not a scan), the current constraint
-- is added only when absent, added NOT VALID so the ADD itself takes no scan, and validated
-- in a separate statement that takes only SHARE UPDATE EXCLUSIVE. A database that already
-- has the validated constraint does none of the three.
ALTER TABLE app.seller_endpoints
  DROP CONSTRAINT IF EXISTS seller_endpoints_retired_when_not_active;

DO $endpoints_constraint$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
      JOIN pg_class t      ON t.oid = c.conrelid
      JOIN pg_namespace n  ON n.oid = t.relnamespace
     WHERE n.nspname = 'app' AND t.relname = 'seller_endpoints'
       AND c.conname = 'seller_endpoints_retirement_matches_status'
  ) THEN
    ALTER TABLE app.seller_endpoints
      ADD CONSTRAINT seller_endpoints_retirement_matches_status
      CHECK ((status = 'active') = (retired_at IS NULL)) NOT VALID;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_constraint c
      JOIN pg_class t      ON t.oid = c.conrelid
      JOIN pg_namespace n  ON n.oid = t.relnamespace
     WHERE n.nspname = 'app' AND t.relname = 'seller_endpoints'
       AND c.conname = 'seller_endpoints_retirement_matches_status'
       AND NOT c.convalidated
  ) THEN
    ALTER TABLE app.seller_endpoints
      VALIDATE CONSTRAINT seller_endpoints_retirement_matches_status;
  END IF;
END
$endpoints_constraint$;

CREATE INDEX IF NOT EXISTS seller_endpoints_store_idx ON app.seller_endpoints (store_id);
-- The lookup the exchange boundary actually performs: this signer's LIVE keys.
CREATE INDEX IF NOT EXISTS seller_endpoints_live_idx
  ON app.seller_endpoints (signer_id, key_id) WHERE status = 'active';

-- D52: the durable store behind T-044's NonceStore port -- seen(signer_id, nonce) and
-- purge_expired(as_of). `retain_until` is set past the auction's respond_by, so a consumed
-- nonce keeps rejecting at every moment before the deadline and is forgotten only after it
-- passes. There is no FK on auction_id on purpose: auction state is Redis-resident with a
-- 15-minute TTL (DESIGN §Data models, Redis), so an FK here would make nonce retention
-- shorter than the auction it outlives, which is precisely the hole D52 closes.
CREATE TABLE IF NOT EXISTS app.bid_nonces (
  bid_nonce_id bigserial   PRIMARY KEY,
  signer_id    text        NOT NULL,
  nonce        text        NOT NULL,
  auction_id   text        NOT NULL,
  consumed_at  timestamptz NOT NULL DEFAULT now(),
  retain_until timestamptz NOT NULL,
  -- The auction's own deadline, recorded on the row so D52's retention property is
  -- CHECKABLE rather than merely asserted in prose. Without it the only constraint
  -- expressible was `retain_until > consumed_at`, which a one-microsecond retention
  -- satisfies -- and a nonce forgotten a microsecond after it is consumed reopens exactly
  -- the replay window D52 exists to close. Nullable rather than NOT NULL because T-044 owns
  -- the writer and its `NonceStore.seen()` signature is frozen without it; when the deadline
  -- IS supplied the constraint below binds.
  respond_by   timestamptz,
  CONSTRAINT bid_nonces_signer_nonce_key UNIQUE (signer_id, nonce),
  CONSTRAINT bid_nonces_retained_past_consumption CHECK (retain_until > consumed_at),
  CONSTRAINT bid_nonces_retained_past_the_auction CHECK (
    respond_by IS NULL OR retain_until > respond_by
  )
);

-- Idempotent repair for a database created by an earlier run of this file. Guarded the same
-- way as seller_endpoints above, and the stakes are higher here: `app.bid_nonces` grows with
-- every signed bid the system ever receives, so an unconditional revalidating ADD CONSTRAINT
-- is a full scan under ACCESS EXCLUSIVE whose cost rises for the life of the deployment.
ALTER TABLE app.bid_nonces ADD COLUMN IF NOT EXISTS respond_by timestamptz;

DO $nonce_constraint$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
      JOIN pg_class t      ON t.oid = c.conrelid
      JOIN pg_namespace n  ON n.oid = t.relnamespace
     WHERE n.nspname = 'app' AND t.relname = 'bid_nonces'
       AND c.conname = 'bid_nonces_retained_past_the_auction'
  ) THEN
    ALTER TABLE app.bid_nonces
      ADD CONSTRAINT bid_nonces_retained_past_the_auction
      CHECK (respond_by IS NULL OR retain_until > respond_by) NOT VALID;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_constraint c
      JOIN pg_class t      ON t.oid = c.conrelid
      JOIN pg_namespace n  ON n.oid = t.relnamespace
     WHERE n.nspname = 'app' AND t.relname = 'bid_nonces'
       AND c.conname = 'bid_nonces_retained_past_the_auction'
       AND NOT c.convalidated
  ) THEN
    ALTER TABLE app.bid_nonces VALIDATE CONSTRAINT bid_nonces_retained_past_the_auction;
  END IF;
END
$nonce_constraint$;

-- purge_expired(as_of) scans by retain_until; seen(signer_id, nonce) uses the unique index.
CREATE INDEX IF NOT EXISTS bid_nonces_retain_until_idx ON app.bid_nonces (retain_until);
CREATE INDEX IF NOT EXISTS bid_nonces_auction_idx      ON app.bid_nonces (auction_id);

-- The blacklist is identity-bound, not store-bound: a re-registered store under a new
-- store_id is still the same business_identity and still blacklisted.
CREATE TABLE IF NOT EXISTS app.seller_blacklist (
  entry_id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  business_identity text        NOT NULL,
  store_id          text        REFERENCES app.sellers (store_id) ON DELETE SET NULL,
  reason_code       text        NOT NULL,
  source            text        NOT NULL,
  status            text        NOT NULL,
  starts_at         timestamptz NOT NULL,
  expires_at        timestamptz,
  reviewed_by       text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT seller_blacklist_status_check CHECK (status IN (
    'active', 'under_review', 'appealed', 'expired'
  )),
  CONSTRAINT seller_blacklist_window_ordered CHECK (expires_at IS NULL OR expires_at >= starts_at)
);

-- Acceptance 1: the blacklist indexes. The lookup is by business_identity, and at most one
-- entry per identity may be live at a time -- the partial unique index is what makes the
-- fail-closed read a single-row question rather than an ordering question.
CREATE INDEX IF NOT EXISTS seller_blacklist_identity_idx
  ON app.seller_blacklist (business_identity);
CREATE UNIQUE INDEX IF NOT EXISTS seller_blacklist_one_live_entry_idx
  ON app.seller_blacklist (business_identity) WHERE status IN ('active', 'under_review', 'appealed');
CREATE INDEX IF NOT EXISTS seller_blacklist_expiry_idx
  ON app.seller_blacklist (expires_at) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS app.buyer_accounts (
  pseudonym  text        PRIMARY KEY,
  buckets    jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT buyer_accounts_buckets_is_object CHECK (jsonb_typeof(buckets) = 'object')
);

CREATE TABLE IF NOT EXISTS app.intents (
  intent_id        text        PRIMARY KEY,
  cluster_id       text,
  pseudonym        text        REFERENCES app.buyer_accounts (pseudonym) ON DELETE SET NULL,
  query            text        NOT NULL,
  category         text,
  hard_constraints jsonb       NOT NULL DEFAULT '[]'::jsonb,
  preferences      jsonb       NOT NULL DEFAULT '[]'::jsonb,
  ship_to          text,
  currency         text,
  budget_band      text,
  schema_version   text        NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT intents_hard_constraints_is_array CHECK (jsonb_typeof(hard_constraints) = 'array'),
  CONSTRAINT intents_preferences_is_array      CHECK (jsonb_typeof(preferences) = 'array')
);

CREATE INDEX IF NOT EXISTS intents_cluster_idx ON app.intents (cluster_id);

CREATE TABLE IF NOT EXISTS app.pitch_requests (
  pitch_request_id text        PRIMARY KEY,
  auction_id       text        NOT NULL,
  intent_id        text        NOT NULL REFERENCES app.intents (intent_id) ON DELETE CASCADE,
  store_id         text        NOT NULL REFERENCES app.sellers (store_id) ON DELETE CASCADE,
  deadline_at      timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pitch_requests_unique_per_store UNIQUE (auction_id, store_id)
);

CREATE INDEX IF NOT EXISTS pitch_requests_deadline_idx ON app.pitch_requests (deadline_at);

CREATE TABLE IF NOT EXISTS app.pitches (
  pitch_id         text        PRIMARY KEY,
  pitch_request_id text        NOT NULL REFERENCES app.pitch_requests (pitch_request_id)
                                 ON DELETE CASCADE,
  store_id         text        NOT NULL REFERENCES app.sellers (store_id) ON DELETE CASCADE,
  product_ref      text,
  body             text        NOT NULL DEFAULT '',
  claims           jsonb       NOT NULL DEFAULT '[]'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pitches_claims_is_array CHECK (jsonb_typeof(claims) = 'array')
);

CREATE INDEX IF NOT EXISTS pitches_request_idx ON app.pitches (pitch_request_id);

CREATE TABLE IF NOT EXISTS app.offers (
  offer_id     text             PRIMARY KEY,
  pitch_id     text             REFERENCES app.pitches (pitch_id) ON DELETE CASCADE,
  store_id     text             NOT NULL REFERENCES app.sellers (store_id) ON DELETE CASCADE,
  product_ref  text             NOT NULL,
  unit_price   numeric(14, 2)   NOT NULL,
  total_price  numeric(14, 2)   NOT NULL,
  discount     jsonb            NOT NULL DEFAULT '{}'::jsonb,
  commitments  jsonb            NOT NULL DEFAULT '[]'::jsonb,
  checkout_url text,
  expires_at   timestamptz,
  created_at   timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT offers_prices_non_negative CHECK (unit_price >= 0 AND total_price >= 0),
  CONSTRAINT offers_commitments_is_array CHECK (jsonb_typeof(commitments) = 'array')
);

CREATE INDEX IF NOT EXISTS offers_store_expiry_idx ON app.offers (store_id, expires_at);
