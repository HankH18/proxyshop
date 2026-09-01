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
-- sealed.* -- seller strategy. Never legible to the auction.
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sealed.envelopes (
  store_id              text        NOT NULL,
  version               integer     NOT NULL,
  activation            text        NOT NULL,
  max_discount_pct      numeric(6, 3),
  budget_cap            numeric(14, 2),
  floors                jsonb       NOT NULL DEFAULT '[]'::jsonb,
  pursue_clusters       jsonb       NOT NULL DEFAULT '[]'::jsonb,
  standing_commitments  jsonb       NOT NULL DEFAULT '[]'::jsonb,
  created_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT envelopes_pkey PRIMARY KEY (store_id, version),
  CONSTRAINT envelopes_version_positive   CHECK (version >= 1),
  CONSTRAINT envelopes_activation_check   CHECK (activation IN ('shadow', 'active', 'killed')),
  CONSTRAINT envelopes_discount_range     CHECK (
    max_discount_pct IS NULL OR (max_discount_pct >= 0 AND max_discount_pct <= 100)
  ),
  CONSTRAINT envelopes_floors_is_array    CHECK (jsonb_typeof(floors) = 'array')
);

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
  CONSTRAINT seller_endpoints_retired_when_not_active CHECK (
    status = 'active' OR retired_at IS NOT NULL
  )
);

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
  CONSTRAINT bid_nonces_signer_nonce_key UNIQUE (signer_id, nonce),
  CONSTRAINT bid_nonces_retained_past_consumption CHECK (retain_until > consumed_at)
);

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
