-- Crypto signals system: initial schema (Slice 1 Step 1.8).
--
-- Tables: scan_runs, signals, agent_runs.
-- The full FR-6.1 schema (active_setups, reasoning_chains, provider_snapshots,
-- strategy_configs, proposed_rules) is added in later steps:
--   active_setups       -> Step 2.8
--   strategy_configs    -> Step 3.2
--   proposed_rules      -> Step 3.6
--   reasoning_chains, provider_snapshots -> as needed by Slice 2-3.
--
-- All DDL here is idempotent (IF NOT EXISTS) so the migration runner is
-- safe to re-execute. SPEC §5.2 requires this for persistence steps.
--
-- pgvector extension is enabled now even though no column uses it yet --
-- Step 3.4 adds a narrative_embedding vector(1536) column to signals;
-- enabling early means the extension is in place when columns reference it.


CREATE EXTENSION IF NOT EXISTS vector;


-- ---------------------------------------------------------------------------
-- scan_runs: one row per scheduled scan execution.
-- Created at scan start; updated to terminal status at completion.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_runs (
    id              UUID        PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ,
    status          TEXT        NOT NULL
                                CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    error_message   TEXT,
    -- Convenience columns for joins / analytics; not in the SPEC §4 Step 1.8
    -- column list but small enough to add now without churn.
    session         TEXT,
    strategy        TEXT,
    symbols         TEXT[]
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_started_at
    ON scan_runs (started_at DESC);


-- ---------------------------------------------------------------------------
-- signals: journal of every analyzer output (published or skipped).
-- Per FR-1.7, skipped reasoning is persisted alongside publications so the
-- Critic can later learn from non-actions. status discriminates the two paths.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id              UUID        PRIMARY KEY,
    scan_id         UUID        NOT NULL
                                REFERENCES scan_runs(id) ON DELETE CASCADE,
    symbol          TEXT        NOT NULL,
    strategy        TEXT        NOT NULL,
    -- NULL for SkipDecision rows (no direction in a skip).
    direction       TEXT        CHECK (direction IS NULL OR direction IN ('LONG', 'SHORT')),
    status          TEXT        NOT NULL
                                CHECK (status IN ('PUBLISHED', 'SKIPPED')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload         JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_scan_id
    ON signals (scan_id);

CREATE INDEX IF NOT EXISTS idx_signals_created_at
    ON signals (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_created_at
    ON signals (symbol, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_signals_status
    ON signals (status);


-- ---------------------------------------------------------------------------
-- Slice 2 Step 2.4 (Historian): promote retrieval columns to first class.
-- Done as ALTER ... ADD COLUMN IF NOT EXISTS (not edits to the base signals
-- definition above) so the migration is idempotent on the ALREADY-DEPLOYED
-- signals table as well as on a fresh install -- the idempotent table create
-- above is a no-op once the table exists, so new columns must be added here.
--   tags             -> Historian stage-2 tag-overlap retrieval (GIN index).
--   features         -> Historian stage-3 numeric L2-distance retrieval.
--   outcome          -> set by the Forecaster (Step 2.9) when a setup closes.
--   outcome_metadata -> free-form details about the outcome (exit price, R, etc.).
ALTER TABLE signals ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS features JSONB NOT NULL DEFAULT '{}'::JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS outcome TEXT
    CHECK (outcome IS NULL OR outcome IN ('WIN', 'LOSS', 'BREAKEVEN', 'INVALIDATED', 'EXPIRED'));
ALTER TABLE signals ADD COLUMN IF NOT EXISTS outcome_metadata JSONB;

CREATE INDEX IF NOT EXISTS idx_signals_tags
    ON signals USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_signals_outcome
    ON signals (outcome);


-- ---------------------------------------------------------------------------
-- agent_runs: per-agent execution log per FR-6.2.
-- Carries the observability + cost data StructuredCompletionResult emits.
-- token_usage is JSONB to accommodate evolving fields (input/output/cache
-- tokens, model name, etc.) without DDL churn.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
    id              UUID        PRIMARY KEY,
    scan_id         UUID        NOT NULL
                                REFERENCES scan_runs(id) ON DELETE CASCADE,
    agent_role      TEXT        NOT NULL
                                CHECK (agent_role IN (
                                    'analyzer',
                                    'historian',
                                    'skeptic',
                                    'judge',
                                    'forecaster',
                                    'critic'
                                )),
    strategy        TEXT,
    input_hash      TEXT        NOT NULL,
    output          JSONB       NOT NULL,
    latency_ms      INTEGER     NOT NULL CHECK (latency_ms >= 0),
    token_usage     JSONB       NOT NULL DEFAULT '{}'::JSONB,
    cost_usd        NUMERIC(12, 6),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_scan_id
    ON agent_runs (scan_id);

CREATE INDEX IF NOT EXISTS idx_agent_runs_role_created_at
    ON agent_runs (agent_role, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runs_created_at
    ON agent_runs (created_at DESC);


-- ---------------------------------------------------------------------------
-- Slice 2 Step 2.8 (active setups): one row per published signal the system is
-- actively tracking. A row is inserted (status OPEN) on a Judge PUBLISH /
-- PUBLISH_WITH_CAVEAT. The Forecaster (Step 2.9) re-evaluates OPEN rows each
-- scan and moves them to a terminal status (mirroring SignalOutcome) when the
-- setup resolves; latest_evaluation holds the Forecaster's most recent
-- STILL_VALID / AT_RISK / INVALIDATED reasoning. signal_id cascades so a deleted
-- signal removes its setup.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS active_setups (
    id                  UUID        PRIMARY KEY,
    signal_id           UUID        NOT NULL
                                    REFERENCES signals(id) ON DELETE CASCADE,
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status              TEXT        NOT NULL
                                    CHECK (status IN (
                                        'OPEN',
                                        'WIN',
                                        'LOSS',
                                        'BREAKEVEN',
                                        'INVALIDATED',
                                        'EXPIRED'
                                    )),
    last_evaluated_at   TIMESTAMPTZ,
    latest_evaluation   JSONB
);

CREATE INDEX IF NOT EXISTS idx_active_setups_status
    ON active_setups (status);

CREATE INDEX IF NOT EXISTS idx_active_setups_signal_id
    ON active_setups (signal_id);


-- ---------------------------------------------------------------------------
-- Slice 2 Step 2.14 -- Critic v0 (design 2026-09-22 §8).
--   decision_resolutions: deterministic price-path replay per decision -- the
--                         journal's ground truth. One row per signal; the
--                         resolver upserts on signal_id so re-runs are idempotent.
--   decision_reviews:     Claude's graded judgement per decision. error is set
--                         when the call failed; the reviewer retries once
--                         (attempts is bumped on the upsert, capped by the queue).
--   critic_reports:       one aggregate report per Critic run (JSONB + markdown,
--                         plus the proposed-rules PR URL once opened).
-- All three cascade from signals so deleting a signal removes its Critic rows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_resolutions (
    signal_id                   UUID        PRIMARY KEY
                                            REFERENCES signals(id) ON DELETE CASCADE,
    decision_kind               TEXT        NOT NULL
                                            CHECK (decision_kind IN ('PUBLISHED', 'VETOED', 'GATE_REJECTED')),
    outcome                     TEXT        NOT NULL
                                            CHECK (outcome IN ('WIN', 'LOSS', 'EXPIRED', 'UNRESOLVABLE')),
    decided_at                  TIMESTAMPTZ NOT NULL,
    entered_at                  TIMESTAMPTZ,
    resolved_at                 TIMESTAMPTZ NOT NULL,
    realized_r                  DOUBLE PRECISION NOT NULL,
    max_adverse_excursion_r     DOUBLE PRECISION,
    max_favorable_excursion_r   DOUBLE PRECISION,
    tp2_reached                 BOOLEAN     NOT NULL DEFAULT FALSE,
    candles_used                INTEGER     NOT NULL CHECK (candles_used >= 0),
    engine_version              TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decision_resolutions_outcome
    ON decision_resolutions (outcome);

CREATE TABLE IF NOT EXISTS decision_reviews (
    signal_id       UUID        PRIMARY KEY
                                REFERENCES signals(id) ON DELETE CASCADE,
    decision_kind   TEXT        NOT NULL
                                CHECK (decision_kind IN ('PUBLISHED', 'VETOED', 'GATE_REJECTED', 'NO_SETUP')),
    review          JSONB       NOT NULL DEFAULT '{}'::JSONB,
    model           TEXT        NOT NULL,
    token_usage     JSONB       NOT NULL DEFAULT '{}'::JSONB,
    cost_usd        NUMERIC(12, 6),
    error           TEXT,
    attempts        INTEGER     NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS critic_reports (
    id              UUID        PRIMARY KEY,
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    report          JSONB       NOT NULL,
    markdown        TEXT        NOT NULL,
    pr_url          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_critic_reports_created_at
    ON critic_reports (created_at DESC);


-- ---------------------------------------------------------------------------
-- Slice 4 Step 4.0 (dashboard design §5.2): setup_evaluations -- one row per
-- Forecaster look at a setup, *including* an ERROR row when the per-setup
-- evaluation raised (those failures previously left no trace in the DB).
-- active_setups.latest_evaluation stays the "last one wins" cache; this table
-- is the full history the dashboard health strip / timeline / coverage read.
-- The Forecaster sweep has no scan_runs row, so it cannot journal through
-- agent_runs (scan_id NOT NULL) -- a dedicated table is the honest shape.
-- Rows cascade from both active_setups and signals.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS setup_evaluations (
    id              UUID        PRIMARY KEY,
    setup_id        UUID        NOT NULL
                                REFERENCES active_setups(id) ON DELETE CASCADE,
    signal_id       UUID        NOT NULL
                                REFERENCES signals(id) ON DELETE CASCADE,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT        NOT NULL
                                CHECK (status IN ('STILL_VALID', 'AT_RISK', 'INVALIDATED', 'ERROR')),
    outcome         TEXT        CHECK (outcome IS NULL OR outcome IN ('WIN', 'LOSS', 'BREAKEVEN', 'INVALIDATED', 'EXPIRED')),
    reasoning       TEXT,
    error           TEXT,
    latency_ms      INTEGER     NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    token_usage     JSONB       NOT NULL DEFAULT '{}'::JSONB,
    cost_usd        NUMERIC(12, 6)
);

CREATE INDEX IF NOT EXISTS idx_setup_evaluations_setup_id_evaluated_at
    ON setup_evaluations (setup_id, evaluated_at DESC);

CREATE INDEX IF NOT EXISTS idx_setup_evaluations_evaluated_at
    ON setup_evaluations (evaluated_at DESC);

-- Dashboard read-optimised indexes (design §5.2): the funnel joins agent_runs
-- on (scan_id, agent_role); the setups page lists by status, newest first.
CREATE INDEX IF NOT EXISTS idx_agent_runs_scan_id_role
    ON agent_runs (scan_id, agent_role);

CREATE INDEX IF NOT EXISTS idx_active_setups_status_opened_at
    ON active_setups (status, opened_at DESC);
