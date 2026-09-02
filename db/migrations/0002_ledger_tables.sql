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
  -- D16 pins timestamps at MILLISECOND precision, and the hash is taken over the
  -- millisecond rendering. `timestamptz` stores microseconds, so a writer that skipped the
  -- library could store .123456, whose canonical form is .123 -- and the row would no
  -- longer hash to its own event_hash. Rejected rather than silently truncated: a value
  -- the writer did not mean to round is a value it should be told about.
  -- (`timezone('UTC', ...)` is IMMUTABLE, which `date_trunc` on a timestamptz is not, so
  -- this is expressible as a CHECK at all.)
  CONSTRAINT commerce_events_occurred_at_is_millisecond CHECK (
    date_trunc('milliseconds', timezone('UTC', occurred_at)) = timezone('UTC', occurred_at)
  ),
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

-- Append-only: UPDATE and DELETE of a written row are refused outright.
--
-- TRUNCATE is deliberately NOT refused, and that is a decision rather than an oversight.
-- Row-level triggers do not fire on TRUNCATE at all, so refusing it would need a separate
-- statement-level trigger -- and TRUNCATE is how the test fixtures reset the chain between
-- tests. What makes the exemption safe is who can use it: TRUNCATE is an owner-only
-- privilege, and 0004 grants it to nobody. `trust_rw` holds SELECT/INSERT/UPDATE/DELETE,
-- `app` holds SELECT/INSERT, `exchange` holds SELECT; none of them can truncate this table.
-- Only the migration runner can, and when it does, the statement-level trigger below resets
-- the anchor with it -- so a truncation is a visible RESET of the chain, never a silent
-- edit to one.
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
-- chain_head -- the commitment that makes TAIL truncation detectable
-- ---------------------------------------------------------------------------------------
-- Without this table, a hash chain cannot detect its own tail being cut off. Delete the
-- last 40 of 100 events and the remaining 60 still verify perfectly: the digest of a
-- truncated chain is a valid chain digest, and there is nothing to compare its LENGTH
-- against. `verify_chain` catches reordering, mid-stream deletion and content tampering --
-- all of which break a link -- and cannot, even in principle, catch truncation from the
-- end. Only a stored commitment can.
--
-- One row, forced by the primary key plus the CHECK on the discriminator. It is maintained
-- by a trigger rather than by the writer, so a row inserted by psql or by some future
-- service keeps it accurate too; a writer that could append without advancing the anchor
-- would be a writer that could truncate without tripping it.
CREATE TABLE IF NOT EXISTS ledger.chain_head (
  chain      text        NOT NULL,
  head_hash  char(64)    NOT NULL,
  length     bigint      NOT NULL,
  last_seq   bigint      NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT chain_head_pkey PRIMARY KEY (chain),
  CONSTRAINT chain_head_is_the_only_chain CHECK (chain = 'commerce_events'),
  CONSTRAINT chain_head_hex               CHECK (head_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT chain_head_length_positive   CHECK (length >= 0)
);

INSERT INTO ledger.chain_head (chain, head_hash, length, last_seq)
  VALUES ('commerce_events', repeat('0', 64), 0, 0)
  ON CONFLICT (chain) DO NOTHING;

-- ---------------------------------------------------------------------------------------
-- The anchor is not re-authorable by the roles it constrains.
--
-- `chain_head` is the ONLY thing that can detect tail truncation, so an attacker who can
-- write it arbitrarily has defeated the mechanism rather than tripped it. Three concrete
-- attacks, every one of them proven live against this schema before this guard existed:
--
--   (B) denial-of-integrity  `UPDATE chain_head SET length = 99` -- a flawless ledger then
--       permanently self-reports {'ok': False, 'reason': 'truncated'}. Unfalsifiable
--       repudiation: the ledger accuses itself and cannot be cleared.
--   (C) silent unanchored appends  `DELETE FROM chain_head` -- the advance function below
--       matched zero rows and returned NULL, so appends kept succeeding with no anchor and
--       no error, and the verifier then RAISED instead of reporting.
--   (D) anchor laundering  re-INSERT an anchor recomputed over the SURVIVORS of a
--       truncation, and the truncated chain verifies {'ok': True, 'anchor_ok': True}.
--
-- 0004 revokes INSERT and DELETE from `trust_rw` and `app`; this trigger is the half that
-- does not depend on a grant being right. The rules:
--
--   DELETE  refused outright. There is no legitimate delete -- the sanctioned reset is an
--           UPDATE, issued by the AFTER TRUNCATE trigger below.
--   INSERT  only the genesis seed row (all-zero head, length 0, last_seq 0), which is what
--           this file's own `ON CONFLICT DO NOTHING` seed inserts, so re-running the
--           migration stays a no-op. (D) is refused because a laundered anchor by
--           definition carries a real head hash and a non-zero length.
--   UPDATE  either the genesis reset -- permitted ONLY while `commerce_events` is actually
--           empty, which outside TRUNCATE nobody can arrange, since the table is
--           append-only and TRUNCATE is owner-only -- or a single-step advance: length
--           exactly +1, last_seq strictly increasing, and `head_hash` equal to the
--           `event_hash` ACTUALLY STORED at `last_seq`. That last clause is a primary-key
--           lookup, so it costs one index probe per append, and it is what binds the
--           commitment to the rows rather than to the writer's say-so. (B) fails the
--           length rule; a forged head fails the row-binding rule.
-- ---------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ledger.chain_head_guard() RETURNS trigger
LANGUAGE plpgsql AS $head_guard$
DECLARE
  chain_is_empty boolean;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION
      'ledger.chain_head is the ledger''s only truncation detector: DELETE is not permitted'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.chain <> 'commerce_events'
       OR NEW.head_hash <> repeat('0', 64)
       OR NEW.length <> 0
       OR NEW.last_seq <> 0 THEN
      RAISE EXCEPTION
        'ledger.chain_head accepts only the genesis seed row on INSERT; an anchor '
        'recomputed over the survivors of a truncation would launder it clean '
        '(offered: head=%, length=%, last_seq=%)', NEW.head_hash, NEW.length, NEW.last_seq
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.chain <> OLD.chain THEN
    RAISE EXCEPTION 'ledger.chain_head.chain is the discriminator and may not be rewritten'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  -- The sanctioned reset (AFTER TRUNCATE, below). Allowed only when the chain really is
  -- empty, so "the anchor says empty" and "the table is empty" can never disagree.
  IF NEW.length = 0 AND NEW.last_seq = 0 AND NEW.head_hash = repeat('0', 64) THEN
    SELECT NOT EXISTS (SELECT 1 FROM ledger.commerce_events) INTO chain_is_empty;
    IF chain_is_empty THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION
      'ledger.chain_head cannot be reset to genesis while ledger.commerce_events still '
      'holds rows: that would make a populated chain report as empty'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF NEW.length <> OLD.length + 1 THEN
    RAISE EXCEPTION
      'ledger.chain_head.length advances by exactly one per appended event: % -> % is not '
      'an append. Rewriting it either fakes events that never happened or makes an intact '
      'ledger report itself truncated.', OLD.length, NEW.length
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF NEW.last_seq <= OLD.last_seq THEN
    RAISE EXCEPTION
      'ledger.chain_head.last_seq is monotonic: % -> % moves the anchor backwards',
      OLD.last_seq, NEW.last_seq
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM ledger.commerce_events
     WHERE seq = NEW.last_seq AND event_hash = NEW.head_hash
  ) THEN
    RAISE EXCEPTION
      'ledger.chain_head.head_hash % is not the event_hash stored at seq %: the anchor must '
      'commit to a row that exists, not to a value the writer chose', NEW.head_hash,
      NEW.last_seq
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  RETURN NEW;
END;
$head_guard$;

CREATE OR REPLACE TRIGGER chain_head_guard_trigger
  BEFORE INSERT OR UPDATE OR DELETE ON ledger.chain_head
  FOR EACH ROW EXECUTE FUNCTION ledger.chain_head_guard();

CREATE OR REPLACE FUNCTION ledger.commerce_events_advance_anchor() RETURNS trigger
LANGUAGE plpgsql AS $anchor$
BEGIN
  UPDATE ledger.chain_head
     SET head_hash  = NEW.event_hash,
         length     = length + 1,
         last_seq   = NEW.seq,
         updated_at = now()
   WHERE chain = 'commerce_events';
  -- NOT FOUND is the whole of attack (C). Without this check the UPDATE simply matched no
  -- rows, returned NULL, and the append committed UNANCHORED -- `inserted = True`, no
  -- error, and the one mechanism that can detect truncation quietly not running. An
  -- append that cannot advance the anchor is an append that must not happen.
  IF NOT FOUND THEN
    RAISE EXCEPTION
      'ledger.chain_head holds no row for the commerce_events chain, so this append could '
      'not advance the anchor. Appending unanchored would leave the ledger with no way to '
      'detect its own tail being cut off; the append is refused instead.'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NULL;
END;
$anchor$;

CREATE OR REPLACE TRIGGER commerce_events_advance_anchor_trigger
  AFTER INSERT ON ledger.commerce_events
  FOR EACH ROW EXECUTE FUNCTION ledger.commerce_events_advance_anchor();

-- The sanctioned reset. TRUNCATE is owner-only (see above), and taking the anchor back to
-- genesis with it is what keeps "the table is empty" and "the chain is empty" the same
-- statement.
CREATE OR REPLACE FUNCTION ledger.commerce_events_reset_anchor() RETURNS trigger
LANGUAGE plpgsql AS $reset$
BEGIN
  UPDATE ledger.chain_head
     SET head_hash  = repeat('0', 64),
         length     = 0,
         last_seq   = 0,
         updated_at = now()
   WHERE chain = 'commerce_events';
  RETURN NULL;
END;
$reset$;

CREATE OR REPLACE TRIGGER commerce_events_reset_anchor_trigger
  AFTER TRUNCATE ON ledger.commerce_events
  FOR EACH STATEMENT EXECUTE FUNCTION ledger.commerce_events_reset_anchor();

-- ---------------------------------------------------------------------------------------
-- Every arm above fires in EVERY session_replication_role, not only in 'origin'
-- ---------------------------------------------------------------------------------------
-- `CREATE TRIGGER` installs a trigger with `pg_trigger.tgenabled = 'O'`, and 'O' means
-- "fires in origin and local mode" -- it does NOT mean "enabled". A session in *replica*
-- mode skips every 'O' trigger on the table, and `session_replication_role` is a plain
-- `SET`: no DDL, no catalog change, nothing left behind, available to any superuser, which
-- is what the migration runner, the admin DSN and any DBA session already are.
--
-- Measured in this tree (worker 22) with the five triggers left at 'O', on a chain with
-- three events in it:
--
--     SET session_replication_role = 'replica';
--     DELETE FROM ledger.chain_head;      -- SUCCEEDED, rowcount = 1
--
-- That is attack (C) above reached in one statement, and it is only the smallest of them:
-- the same switch turns off the append-only arm, the prev_hash link check and the anchor
-- advance at once, so the follow-up is to rewrite `commerce_events` row by row and leave
-- behind a chain that still verifies against an anchor that was never updated.
--
-- All five are INTEGRITY triggers, not replication-aware ones. `session_replication_role`
-- exists so that a subscription applying another node's already-validated changes does not
-- re-run the origin's business logic; none of these five is business logic, this database
-- is not a subscriber to anything, and there is no arrangement in which skipping them is
-- correct. `ENABLE ALWAYS` (tgenabled = 'A') is the state that says exactly that, and it
-- is what the catalog test in apps/trust/tests/test_ledger_chain.py pins.
--
-- What this does NOT claim: the table owner can still `ALTER TABLE ... DISABLE TRIGGER`.
-- The difference that matters is that DISABLE is DDL -- it takes an ACCESS EXCLUSIVE lock
-- and it moves `tgenabled` to 'D', where the catalog test sees it -- whereas a GUC is
-- invisible to every catalog query there is.
ALTER TABLE ledger.chain_head
  ENABLE ALWAYS TRIGGER chain_head_guard_trigger;
ALTER TABLE ledger.commerce_events
  ENABLE ALWAYS TRIGGER commerce_events_chain_guard_trigger;
ALTER TABLE ledger.commerce_events
  ENABLE ALWAYS TRIGGER commerce_events_append_only_trigger;
ALTER TABLE ledger.commerce_events
  ENABLE ALWAYS TRIGGER commerce_events_advance_anchor_trigger;
ALTER TABLE ledger.commerce_events
  ENABLE ALWAYS TRIGGER commerce_events_reset_anchor_trigger;

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
