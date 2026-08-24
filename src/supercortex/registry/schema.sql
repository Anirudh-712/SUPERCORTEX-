-- Project Supercortex — the registry.
--
-- This file is the spine of the desk. Its single most important property is that
-- `trial` is APPEND-ONLY: every backtest that was ever executed leaves a row, including
-- the ones that were abandoned. The Deflated Sharpe Ratio is only honest if that count
-- is honest, so the database refuses to let anything rewrite history.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── identity ────────────────────────────────────────────────────────────────────
-- Monotonic per-prefix counters. IDs are never reused, even for deleted entities.
CREATE TABLE IF NOT EXISTS id_counter (
    prefix TEXT PRIMARY KEY,           -- EXP, STR, DEC, LRN, DATA, AGT, DEP, IDEA, ADR, INC
    next_value INTEGER NOT NULL
);

-- ── experiments ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS experiment (
    id             TEXT PRIMARY KEY,   -- EXP-000042
    title          TEXT NOT NULL,
    slug           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'RESEARCH',
    hypothesis_id  TEXT,
    strategy_id    TEXT,
    -- Declared BEFORE research begins. Overruns are allowed but recorded, and they
    -- inflate the multiple-testing penalty rather than being quietly forgotten.
    trial_budget   INTEGER NOT NULL,
    market         TEXT NOT NULL DEFAULT 'crypto',
    created_at     TEXT NOT NULL,
    owner_agent    TEXT,
    CHECK (trial_budget > 0),
    CHECK (status IN ('IDEA','CANDIDATE','RESEARCH','BACKTESTING','VALIDATING',
                      'FALSIFYING','PAPER','APPROVED','LIVE','DEGRADED',
                      'SUSPENDED','RETIRED','KILLED'))
);

-- ── the counting machine ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trial (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id      TEXT NOT NULL REFERENCES experiment(id),
    seq                INTEGER NOT NULL,
    config_hash        TEXT NOT NULL,   -- sha256 of the fully resolved config
    code_commit        TEXT NOT NULL,   -- git HEAD; a dirty tree refuses to run
    data_snapshot      TEXT NOT NULL,   -- sha256 over the dataset manifest
    cost_model_version TEXT NOT NULL,
    seed               INTEGER NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    outcome            TEXT,            -- ok | error | abandoned  (all three count)
    metrics_path       TEXT,
    agent_id           TEXT,
    note               TEXT,
    UNIQUE (experiment_id, seq),
    CHECK (outcome IS NULL OR outcome IN ('ok','error','abandoned'))
);
CREATE INDEX IF NOT EXISTS ix_trial_experiment ON trial(experiment_id);

-- History is immutable. These three triggers are the reason the DSR can be trusted.
CREATE TRIGGER IF NOT EXISTS trial_no_delete
BEFORE DELETE ON trial
BEGIN
    SELECT RAISE(ABORT, 'trial is append-only: a deleted trial is a falsified experiment');
END;

CREATE TRIGGER IF NOT EXISTS trial_no_reseal
BEFORE UPDATE ON trial
WHEN OLD.finished_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'trial already sealed: finished trials cannot be modified');
END;

CREATE TRIGGER IF NOT EXISTS trial_identity_immutable
BEFORE UPDATE ON trial
WHEN NEW.experiment_id <> OLD.experiment_id
  OR NEW.seq           <> OLD.seq
  OR NEW.config_hash   <> OLD.config_hash
  OR NEW.code_commit   <> OLD.code_commit
  OR NEW.data_snapshot <> OLD.data_snapshot
  OR NEW.started_at    <> OLD.started_at
BEGIN
    SELECT RAISE(ABORT, 'trial identity is immutable: only completion fields may be set');
END;

-- ── falsification verdicts ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS falsification (
    experiment_id TEXT NOT NULL REFERENCES experiment(id),
    test_name     TEXT NOT NULL,
    statistic     REAL,
    threshold     REAL,
    passed        INTEGER NOT NULL,
    ran_at        TEXT NOT NULL,
    artifact_path TEXT,
    trials_at_run INTEGER NOT NULL,   -- the N that fed the DSR at verdict time
    PRIMARY KEY (experiment_id, test_name)
);

-- ── lifecycle ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lifecycle_event (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id        TEXT NOT NULL,
    from_state       TEXT,
    to_state         TEXT NOT NULL,
    gate_report_path TEXT,
    approved_by      TEXT,            -- 'human:anirudh' for gated transitions
    at               TEXT NOT NULL
);

-- ── supporting registers ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dataset (
    id          TEXT PRIMARY KEY,     -- DATA-000003
    title       TEXT NOT NULL,
    venue       TEXT,
    symbol      TEXT,
    kind        TEXT,                 -- klines | funding | metrics
    interval    TEXT,
    parquet_path TEXT,                -- the exact L1 file; verify never guesses
    sha256      TEXT NOT NULL,
    rows        INTEGER,
    start_ts    TEXT,
    end_ts      TEXT,
    schema_path TEXT,
    gaps_json   TEXT,
    verified_at TEXT,
    card_path   TEXT
);

CREATE TABLE IF NOT EXISTS decision (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, note_path TEXT NOT NULL,
    approved_by TEXT, at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, note_path TEXT NOT NULL,
    source_experiment TEXT, at TEXT NOT NULL
);

-- ── views ───────────────────────────────────────────────────────────────────────
-- The number that feeds the Deflated Sharpe Ratio. Counts everything, including
-- errors and abandoned runs, because every one of them was a look at the data.
CREATE VIEW IF NOT EXISTS experiment_trials AS
SELECT e.id,
       e.title,
       e.status,
       e.trial_budget,
       COUNT(t.id)                                              AS trials_consumed,
       SUM(CASE WHEN t.outcome = 'ok'        THEN 1 ELSE 0 END) AS trials_ok,
       SUM(CASE WHEN t.outcome = 'abandoned' THEN 1 ELSE 0 END) AS trials_abandoned,
       SUM(CASE WHEN t.outcome IS NULL       THEN 1 ELSE 0 END) AS trials_open,
       CASE WHEN COUNT(t.id) > e.trial_budget THEN 1 ELSE 0 END AS over_budget
FROM experiment e
LEFT JOIN trial t ON t.experiment_id = e.id
GROUP BY e.id;
