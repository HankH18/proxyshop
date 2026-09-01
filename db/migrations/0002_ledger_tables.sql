-- 0002 — the append-only, hash-chained ledger schema (DESIGN §Data models, D16).
-- Owned by T-011.
--
-- The partner-reconciled table set, in dependency order:
--   commerce_events               the single global hash chain (D16)
--   catalog_snapshots             the catalog state a verification decision was made against
--   claims / claim_verifications / verification_evidence_refs
--   trust_observations / trust_scores
--   policy_events
--   ranking_runs / ranking_candidates
--   crawl_jobs / crawl_pages
--
-- D16, restated because it is the whole point of this file: ONE global chain over
-- ledger.commerce_events ordered by insertion sequence;
-- `event_hash = sha256(prev_hash || canonical_json(event))` with canonical_json = RFC-8785
-- JCS and timestamps UTC RFC-3339 at millisecond precision; and
-- `commerce_events.idempotency_key` IS `LedgerEvent.event_id` -- there is no second
-- identifier. The hashing itself lives in Python, in apps/trust/src/ledger/, and nowhere
-- else. What the database enforces is the three properties Python cannot enforce alone:
-- the key is unique, the link is valid, and a written row is never rewritten.

-- ---------------------------------------------------------------------------------------
-- commerce_events -- the chain
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger.commerce_events (
  seq              bigserial     PRIMARY KEY,
  -- D16: this IS LedgerEvent.event_id. The UNIQUE constraint is the idempotency guarantee;
  -- the writer relies on it with ON CONFLICT DO NOTHING, so a re-delivered event is a
  -- genuine no-op rather than a second row with a second hash.
  idempotency_key  text          NOT NULL,
  kind             text          NOT NULL,
  auction_id       text,
  store_id         text,
  order_ref        text,
  -- LedgerEvent.ts. Stored to millisecond precision so the value that comes back out of
  -- the database canonicalizes to the byte string that went into the hash.
  occurred_at      timestamptz   NOT NULL,
  recorded_at      timestamptz   NOT NULL DEFAULT now(),
  payload          jsonb         NOT NULL DEFAULT '{}'::jsonb,
  prev_hash        char(64)      NOT NULL,
  event_hash       char(64)      NOT NULL,
  CONSTRAINT commerce_events_idempotency_key_key UNIQUE (idempotency_key),
  CONSTRAINT commerce_events_event_hash_key      UNIQUE (event_hash),
  -- D16 says ONE global chain. Two rows sharing a prev_hash is a fork, and a fork is the
  -- failure a hash chain exists to make impossible, so it is a constraint rather than a
  -- convention: even a writer that skipped the advisory lock cannot branch the chain.
  CONSTRAINT commerce_events_prev_hash_key       UNIQUE (prev_hash),
  CONSTRAINT commerce_events_prev_hash_hex       CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT commerce_events_event_hash_hex      CHECK (event_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT commerce_events_not_self_linked     CHECK (prev_hash <> event_hash),
  CONSTRAINT commerce_events_payload_is_object   CHECK (jsonb_typeof(payload) = 'object'),
  -- C11 / D24: the frozen LedgerEvent kind vocabulary. Thirteen from DESIGN §Interfaces
  -- plus the five D24 added in T-010, and no others.
  CONSTRAINT commerce_events_kind_check CHECK (kind IN (
    'bid_placed', 'shown', 'accepted', 'code_created', 'checkout_redirect',
    'checkout_pixel', 'order_paid', 'order_fulfilled', 'refund', 'feedback',
    'reconciled', 'claim_verified', 'policy_event',
    'auction_opened', 'auction_closed', 'offer_integrity', 'blacklisted',
    'blacklist_expired'
  ))
);

CREATE INDEX IF NOT EXISTS commerce_events_store_seq_idx
  ON ledger.commerce_events (store_id, seq);
CREATE INDEX IF NOT EXISTS commerce_events_auction_seq_idx
  ON ledger.commerce_events (auction_id, seq);
CREATE INDEX IF NOT EXISTS commerce_events_kind_seq_idx
  ON ledger.commerce_events (kind, seq);
CREATE INDEX IF NOT EXISTS commerce_events_order_ref_idx
  ON ledger.commerce_events (order_ref);

-- The link check. A row whose prev_hash is not the current tail's event_hash is refused,
-- so a chain cannot be forked or back-dated even by a writer that skipped the library.
CREATE OR REPLACE FUNCTION ledger.commerce_events_chain_guard() RETURNS trigger
LANGUAGE plpgsql AS $chain$
DECLARE
  tail_hash char(64);
BEGIN
  SELECT c.event_hash INTO tail_hash
    FROM ledger.commerce_events c
   ORDER BY c.seq DESC
   LIMIT 1;
  IF tail_hash IS NULL THEN
    tail_hash := repeat('0', 64);
  END IF;
  IF NEW.prev_hash <> tail_hash THEN
    RAISE EXCEPTION
      'ledger.commerce_events: prev_hash % does not link to the chain tail %',
      NEW.prev_hash, tail_hash
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END;
$chain$;

CREATE OR REPLACE TRIGGER commerce_events_chain_guard_trigger
  BEFORE INSERT ON ledger.commerce_events
  FOR EACH ROW EXECUTE FUNCTION ledger.commerce_events_chain_guard();

-- Append-only. TRUNCATE deliberately still works (it fires no row trigger), because that
-- is how a test resets the fixture; UPDATE and DELETE of a written row never do.
CREATE OR REPLACE FUNCTION ledger.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $immutable$
BEGIN
  RAISE EXCEPTION
    'ledger.commerce_events is append-only: % is not permitted', TG_OP
    USING ERRCODE = 'integrity_constraint_violation';
END;
$immutable$;

CREATE OR REPLACE TRIGGER commerce_events_append_only_trigger
  BEFORE UPDATE OR DELETE ON ledger.commerce_events
  FOR EACH ROW EXECUTE FUNCTION ledger.reject_mutation();

-- ---------------------------------------------------------------------------------------
-- catalog_snapshots -- what a verification decision was made against
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger.catalog_snapshots (
  snapshot_id       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  store_id          text        NOT NULL,
  snapshot_ref      text        NOT NULL,
  content_hash      text        NOT NULL,
  extractor_version text        NOT NULL,
  observed_at       timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT catalog_snapshots_store_content_key UNIQUE (store_id, content_hash)
);

CREATE INDEX IF NOT EXISTS catalog_snapshots_store_observed_idx
  ON ledger.catalog_snapshots (store_id, observed_at DESC);

-- ---------------------------------------------------------------------------------------
-- claims / claim_verifications / verification_evidence_refs
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger.claims (
  claim_id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  event_seq         bigint      REFERENCES ledger.commerce_events (seq),
  store_id          text        NOT NULL,
  pitch_ref         text,
  product_ref       text,
  claim_ref         text        NOT NULL,
  -- The published claim_type vocabulary (DESIGN §Decisions, claim_type -> dimension). The
  -- mapping itself is read from fixtures/manifest.json by T-062; only membership is
  -- constrained here, so an unmapped type cannot be persisted in the first place.
  claim_type        text        NOT NULL,
  key               text        NOT NULL,
  value             jsonb,
  provenance_source text        NOT NULL,
  provenance_ref    text,
  authority_rank    integer     NOT NULL DEFAULT 0,
  observed_at       timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT claims_store_claim_ref_key UNIQUE (store_id, claim_ref),
  CONSTRAINT claims_provenance_source_check CHECK (provenance_source IN (
    'scraped', 'pixel_feed', 'owner_statement', 'envelope_rule',
    'learned_policy', 'network', 'seller_asserted'
  )),
  CONSTRAINT claims_claim_type_check CHECK (claim_type IN (
    'price', 'unit_price', 'total_price',
    'discount', 'promo_eligibility',
    'delivery', 'shipping_speed', 'dispatch_window',
    'return_policy', 'warranty',
    'ingredients', 'compatibility', 'nutrition', 'specifications'
  ))
);

CREATE INDEX IF NOT EXISTS claims_store_observed_idx ON ledger.claims (store_id, observed_at);
CREATE INDEX IF NOT EXISTS claims_pitch_ref_idx      ON ledger.claims (pitch_ref);
CREATE INDEX IF NOT EXISTS claims_event_seq_idx      ON ledger.claims (event_seq);

CREATE TABLE IF NOT EXISTS ledger.claim_verifications (
  verification_id     uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id            uuid             NOT NULL REFERENCES ledger.claims (claim_id)
                                         ON DELETE RESTRICT,
  catalog_snapshot_id uuid             NOT NULL REFERENCES ledger.catalog_snapshots (snapshot_id)
                                         ON DELETE RESTRICT,
  verifier_version    text             NOT NULL,
  status              text             NOT NULL,
  observed_value      jsonb,
  confidence          double precision NOT NULL,
  -- The trust dimension this outcome lands on, per the published claim_type table.
  dim                 text,
  event_seq           bigint           REFERENCES ledger.commerce_events (seq),
  verified_at         timestamptz      NOT NULL,
  created_at          timestamptz      NOT NULL DEFAULT now(),
  -- T-065 acceptance 2: re-running the same (claim, snapshot, verifier version) writes
  -- nothing; a snapshot bump is a different row and therefore re-verifies.
  CONSTRAINT claim_verifications_idempotency_key
    UNIQUE (claim_id, catalog_snapshot_id, verifier_version),
  CONSTRAINT claim_verifications_status_check CHECK (status IN (
    'verified', 'contradicted', 'unsupported', 'ambiguous'
  )),
  CONSTRAINT claim_verifications_confidence_range
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
  CONSTRAINT claim_verifications_dim_check CHECK (dim IS NULL OR dim IN (
    'price_honored', 'discount_honored', 'shipped_on_time',
    'not_returned', 'feedback_match', 'catalog_claim_accuracy'
  ))
);

CREATE INDEX IF NOT EXISTS claim_verifications_claim_idx
  ON ledger.claim_verifications (claim_id);
CREATE INDEX IF NOT EXISTS claim_verifications_snapshot_idx
  ON ledger.claim_verifications (catalog_snapshot_id);

CREATE TABLE IF NOT EXISTS ledger.verification_evidence_refs (
  evidence_id     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  verification_id uuid        NOT NULL REFERENCES ledger.claim_verifications (verification_id)
                                ON DELETE CASCADE,
  evidence_ref    text        NOT NULL,
  source_class    text,
  content_hash    text,
  observed_at     timestamptz,
  CONSTRAINT verification_evidence_refs_unique UNIQUE (verification_id, evidence_ref),
  CONSTRAINT verification_evidence_refs_source_class_check CHECK (
    source_class IS NULL OR source_class IN (
      'scraped', 'pixel_feed', 'owner_statement', 'envelope_rule',
      'learned_policy', 'network', 'seller_asserted'
    )
  )
);

CREATE INDEX IF NOT EXISTS verification_evidence_refs_verification_idx
  ON ledger.verification_evidence_refs (verification_id);

-- ---------------------------------------------------------------------------------------
-- trust_observations / trust_scores
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger.trust_observations (
  observation_id   uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
  event_seq        bigint           REFERENCES ledger.commerce_events (seq),
  verification_id  uuid             REFERENCES ledger.claim_verifications (verification_id)
                                      ON DELETE RESTRICT,
  store_id         text             NOT NULL,
  dim              text             NOT NULL,
  observation_type text             NOT NULL,
  weight           double precision,
  observed_at      timestamptz      NOT NULL,
  created_at       timestamptz      NOT NULL DEFAULT now(),
  -- DESIGN §Interfaces TrustSnapshot: exactly six dimensions inside one trust system.
  CONSTRAINT trust_observations_dim_check CHECK (dim IN (
    'price_honored', 'discount_honored', 'shipped_on_time',
    'not_returned', 'feedback_match', 'catalog_claim_accuracy'
  )),
  CONSTRAINT trust_observations_type_check CHECK (observation_type IN (
    'verified', 'contradicted', 'unsupported', 'ambiguous',
    'fulfilled', 'mismatch_return', 'severe_policy'
  ))
);

CREATE INDEX IF NOT EXISTS trust_observations_store_dim_observed_idx
  ON ledger.trust_observations (store_id, dim, observed_at);
CREATE INDEX IF NOT EXISTS trust_observations_event_seq_idx
  ON ledger.trust_observations (event_seq);

CREATE TABLE IF NOT EXISTS ledger.trust_scores (
  store_id                text             NOT NULL,
  snapshot_version        integer          NOT NULL,
  score                   double precision NOT NULL,
  confidence              double precision NOT NULL,
  effective_sample_size   double precision NOT NULL DEFAULT 0.0,
  score_version           text             NOT NULL,
  -- The last LedgerEvent.event_id folded in, which is what makes the S3 replay assertion
  -- checkable against a served snapshot. FK to the chain by its idempotency key -- D16
  -- says that key IS the event id, so this is the only sound target.
  computed_through_event  text             REFERENCES ledger.commerce_events (idempotency_key),
  low_data                boolean          NOT NULL DEFAULT true,
  blacklisted             boolean          NOT NULL DEFAULT false,
  dims                    jsonb            NOT NULL,
  as_of                   timestamptz      NOT NULL,
  computed_at             timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT trust_scores_pkey PRIMARY KEY (store_id, snapshot_version),
  CONSTRAINT trust_scores_score_range      CHECK (score >= 0.0 AND score <= 1.0),
  CONSTRAINT trust_scores_confidence_range CHECK (confidence >= 0.0 AND confidence <= 1.0),
  CONSTRAINT trust_scores_dims_is_object   CHECK (jsonb_typeof(dims) = 'object')
);

CREATE INDEX IF NOT EXISTS trust_scores_store_computed_idx
  ON ledger.trust_scores (store_id, computed_at DESC);

-- ---------------------------------------------------------------------------------------
-- policy_events -- the penalty source for the published rank formula
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger.policy_events (
  policy_event_id uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
  event_seq       bigint           REFERENCES ledger.commerce_events (seq),
  store_id        text             NOT NULL,
  kind            text             NOT NULL,
  penalty         double precision NOT NULL DEFAULT 0.0,
  details         jsonb            NOT NULL DEFAULT '{}'::jsonb,
  opened_at       timestamptz      NOT NULL,
  closed_at       timestamptz,
  CONSTRAINT policy_events_penalty_non_negative CHECK (penalty >= 0.0),
  CONSTRAINT policy_events_window_ordered CHECK (closed_at IS NULL OR closed_at >= opened_at)
);

-- `policy_penalties` sums OPEN policy events for a store in the scoring window, so the
-- partial index is the shape that query actually uses.
CREATE INDEX IF NOT EXISTS policy_events_open_by_store_idx
  ON ledger.policy_events (store_id, opened_at) WHERE closed_at IS NULL;

-- ---------------------------------------------------------------------------------------
-- ranking_runs / ranking_candidates
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger.ranking_runs (
  run_id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  auction_id      text        NOT NULL,
  weights_version text        NOT NULL,
  formula_version text        NOT NULL,
  ranked_at       timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ranking_runs_auction_idx ON ledger.ranking_runs (auction_id, ranked_at DESC);

CREATE TABLE IF NOT EXISTS ledger.ranking_candidates (
  run_id            uuid             NOT NULL REFERENCES ledger.ranking_runs (run_id)
                                       ON DELETE CASCADE,
  bid_ref           text             NOT NULL,
  store_id          text             NOT NULL,
  eligible          boolean          NOT NULL,
  rank_score        double precision,
  components        jsonb            NOT NULL DEFAULT '{}'::jsonb,
  exclusion_reasons jsonb            NOT NULL DEFAULT '[]'::jsonb,
  position          integer,
  CONSTRAINT ranking_candidates_pkey PRIMARY KEY (run_id, bid_ref),
  CONSTRAINT ranking_candidates_components_is_object CHECK (jsonb_typeof(components) = 'object'),
  CONSTRAINT ranking_candidates_exclusions_is_array  CHECK (jsonb_typeof(exclusion_reasons) = 'array'),
  -- S8-1: an excluded candidate always records why. An ineligible row with an empty
  -- reason list is the exact shape the release blocker exists to prevent.
  CONSTRAINT ranking_candidates_ineligible_has_reason
    CHECK (eligible OR jsonb_array_length(exclusion_reasons) > 0)
);

CREATE INDEX IF NOT EXISTS ranking_candidates_store_idx ON ledger.ranking_candidates (store_id);

-- ---------------------------------------------------------------------------------------
-- crawl_jobs / crawl_pages
-- ---------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger.crawl_jobs (
  job_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  store_id      text        NOT NULL,
  status        text        NOT NULL,
  robots_policy text,
  requested_at  timestamptz NOT NULL,
  started_at    timestamptz,
  finished_at   timestamptz,
  error         text,
  CONSTRAINT crawl_jobs_status_check CHECK (status IN (
    'queued', 'running', 'succeeded', 'failed', 'skipped'
  ))
);

CREATE INDEX IF NOT EXISTS crawl_jobs_store_requested_idx
  ON ledger.crawl_jobs (store_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS ledger.crawl_pages (
  page_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id       uuid        NOT NULL REFERENCES ledger.crawl_jobs (job_id) ON DELETE CASCADE,
  url          text        NOT NULL,
  content_hash text        NOT NULL,
  http_status  integer,
  snapshot_ref text,
  fetched_at   timestamptz NOT NULL,
  CONSTRAINT crawl_pages_job_url_key UNIQUE (job_id, url)
);

CREATE INDEX IF NOT EXISTS crawl_pages_content_hash_idx ON ledger.crawl_pages (content_hash);
