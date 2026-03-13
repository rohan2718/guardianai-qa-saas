-- ══════════════════════════════════════════════════════════════════════════════
-- GuardianAI — Autonomous QA Schema Migration
-- Run once against existing database.
-- All tables cascade-delete when the parent test_run is deleted.
-- ══════════════════════════════════════════════════════════════════════════════

-- ── 1. qa_flows — discovered user journey flows ───────────────────────────────
CREATE TABLE IF NOT EXISTS qa_flows (
    id          SERIAL PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    flow_id     VARCHAR(64) NOT NULL,          -- e.g. "flow_login_001"
    flow_name   TEXT NOT NULL,
    flow_type   VARCHAR(30),                   -- login|checkout|navigation|...
    priority    VARCHAR(10),                   -- critical|high|medium|low
    entry_url   TEXT,
    exit_url    TEXT,
    description TEXT,
    tags        JSONB DEFAULT '[]',
    steps       JSONB DEFAULT '[]',            -- list of FlowStep dicts
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_qaflow_run_id   ON qa_flows(run_id);
CREATE INDEX IF NOT EXISTS ix_qaflow_priority ON qa_flows(priority);


-- ── 2. qa_test_cases — generated test cases from flows ───────────────────────
CREATE TABLE IF NOT EXISTS qa_test_cases (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    flow_id             VARCHAR(64),
    qa_flow_db_id       INTEGER REFERENCES qa_flows(id) ON DELETE SET NULL,
    tc_id               VARCHAR(30) NOT NULL,  -- e.g. "TC-42-001"
    scenario            TEXT NOT NULL,
    description         TEXT,
    preconditions       JSONB DEFAULT '[]',
    steps               JSONB DEFAULT '[]',    -- list of TestStep dicts
    expected_result     TEXT,
    actual_result       TEXT,
    status              VARCHAR(10) DEFAULT 'pending',  -- pending|pass|fail|blocked|skip
    severity            VARCHAR(10) DEFAULT 'medium',
    tags                JSONB DEFAULT '[]',
    playwright_snippet  TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_qatc_run_id ON qa_test_cases(run_id);
CREATE INDEX IF NOT EXISTS ix_qatc_status ON qa_test_cases(status);
CREATE INDEX IF NOT EXISTS ix_qatc_severity ON qa_test_cases(severity);


-- ── 3. qa_test_results — execution results per test case ────────────────────
CREATE TABLE IF NOT EXISTS qa_test_results (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    test_case_id    INTEGER REFERENCES qa_test_cases(id) ON DELETE CASCADE,
    tc_id           VARCHAR(30) NOT NULL,
    flow_id         VARCHAR(64),
    scenario        TEXT,
    status          VARCHAR(10),               -- pass|fail|error|timeout|skip
    actual_result   TEXT,
    failure_step    INTEGER,
    failure_reason  TEXT,
    duration_ms     FLOAT,
    screenshot_path TEXT,
    step_results    JSONB DEFAULT '[]',        -- list of StepResult dicts
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_qatestresult_run_id ON qa_test_results(run_id);
CREATE INDEX IF NOT EXISTS ix_qatestresult_status ON qa_test_results(status);


-- ── 4. bug_reports — all bugs (scan-based + test-failure-based) ──────────────
CREATE TABLE IF NOT EXISTS bug_reports (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    page_result_id      INTEGER REFERENCES page_results(id) ON DELETE SET NULL,
    test_case_id        INTEGER REFERENCES qa_test_cases(id) ON DELETE SET NULL,
    tc_id               VARCHAR(30),
    flow_id             VARCHAR(64),
    bug_title           TEXT NOT NULL,
    page_url            TEXT,
    bug_type            VARCHAR(30),           -- performance|security|accessibility|functional|navigation|interaction
    severity            VARCHAR(10) NOT NULL,  -- critical|high|medium|low
    component           TEXT,
    description         TEXT,
    impact              TEXT,
    steps_to_reproduce  JSONB DEFAULT '[]',
    expected_result     TEXT,
    actual_result       TEXT,
    suggested_fix       TEXT,
    screenshot_path     TEXT,
    source              VARCHAR(20) DEFAULT 'scan',  -- scan|test_runner
    is_resolved         BOOLEAN DEFAULT FALSE,
    playwright_snippet  TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_bugreport_run_id   ON bug_reports(run_id);
CREATE INDEX IF NOT EXISTS ix_bugreport_severity ON bug_reports(severity);
CREATE INDEX IF NOT EXISTS ix_bugreport_source   ON bug_reports(source);
CREATE INDEX IF NOT EXISTS ix_bugreport_type     ON bug_reports(bug_type);


-- ── 5. regression_reports — scan comparison results ─────────────────────────
CREATE TABLE IF NOT EXISTS regression_reports (
    id              SERIAL PRIMARY KEY,
    run_id_before   INTEGER REFERENCES test_runs(id) ON DELETE SET NULL,
    run_id_after    INTEGER REFERENCES test_runs(id) ON DELETE SET NULL,
    health_before   FLOAT,
    health_after    FLOAT,
    health_delta    FLOAT,
    bugs_before     INTEGER DEFAULT 0,
    bugs_after      INTEGER DEFAULT 0,
    bugs_new        INTEGER DEFAULT 0,
    bugs_resolved   INTEGER DEFAULT 0,
    bugs_persisting INTEGER DEFAULT 0,
    verdict         VARCHAR(20),               -- improved|degraded|unchanged
    summary         TEXT,
    report_data     JSONB,                     -- full RegressionReport dict
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_regression_run_after ON regression_reports(run_id_after);


-- ── 6. Add QA aggregate columns to test_runs ─────────────────────────────────
-- These allow the dashboard to show QA stats without joining child tables.

ALTER TABLE test_runs
    ADD COLUMN IF NOT EXISTS total_bugs       INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS critical_bugs    INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS high_bugs        INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS medium_bugs      INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS low_bugs         INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_flows      INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_test_cases INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tests_passed     INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tests_failed     INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS qa_enabled       BOOLEAN DEFAULT FALSE;

-- ══════════════════════════════════════════════════════════════════════════════
-- ROLLBACK SCRIPT (run if you need to revert)
-- ══════════════════════════════════════════════════════════════════════════════
-- DROP TABLE IF EXISTS regression_reports CASCADE;
-- DROP TABLE IF EXISTS bug_reports CASCADE;
-- DROP TABLE IF EXISTS qa_test_results CASCADE;
-- DROP TABLE IF EXISTS qa_test_cases CASCADE;
-- DROP TABLE IF EXISTS qa_flows CASCADE;
-- ALTER TABLE test_runs
--     DROP COLUMN IF EXISTS total_bugs,
--     DROP COLUMN IF EXISTS critical_bugs,
--     DROP COLUMN IF EXISTS high_bugs,
--     DROP COLUMN IF EXISTS medium_bugs,
--     DROP COLUMN IF EXISTS low_bugs,
--     DROP COLUMN IF EXISTS total_flows,
--     DROP COLUMN IF EXISTS total_test_cases,
--     DROP COLUMN IF EXISTS tests_passed,
--     DROP COLUMN IF EXISTS tests_failed,
--     DROP COLUMN IF EXISTS qa_enabled;