"""
Migration script — adds all new columns to existing tables.
Run ONCE: python migrate_db.py
Safe to re-run — skips columns that already exist.
"""

from dotenv import load_dotenv
load_dotenv()

import os
import psycopg2

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", 5432)),
    dbname=os.environ.get("DB_NAME", "qa_system"),
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASS", ""),
)
cur = conn.cursor()

# ── All new columns for test_runs ──────────────────────────────────────────────
migrations = [
    # column_name,              data_type,              default
    ("discovered_pages",        "INTEGER",              "DEFAULT 0"),
    ("avg_scan_time_ms",        "FLOAT",                ""),
    ("eta_seconds",             "FLOAT",                ""),
    ("scan_filters",            "TEXT",                 ""),
    ("confidence_score",        "FLOAT",                ""),
]

print("Migrating test_runs table...")
for col, dtype, default in migrations:
    try:
        sql = f"ALTER TABLE test_runs ADD COLUMN {col} {dtype} {default};"
        cur.execute(sql)
        conn.commit()
        print(f"  ✓ Added column: {col}")
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()
        print(f"  — Already exists: {col}")
    except Exception as e:
        conn.rollback()
        print(f"  ✗ Failed {col}: {e}")

# ── Create page_results table if not exists ────────────────────────────────────
print("\nCreating page_results table (if not exists)...")
try:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS page_results (
            id                      SERIAL PRIMARY KEY,
            run_id                  INTEGER REFERENCES test_runs(id),
            url                     VARCHAR(1000),
            title                   VARCHAR(500),
            scanned_at              TIMESTAMPTZ,
            status                  INTEGER,
            health_score            FLOAT,
            risk_category           VARCHAR(50),
            performance_score       FLOAT,
            accessibility_score     FLOAT,
            security_score          FLOAT,
            functional_score        FLOAT,
            ui_form_score           FLOAT,
            confidence_score        FLOAT,
            checks_executed         INTEGER,
            checks_null             INTEGER,
            failure_pattern_id      VARCHAR(64),
            root_cause_tag          VARCHAR(200),
            similar_issue_ref       INTEGER,
            ai_confidence           FLOAT,
            self_healing_suggestion TEXT,
            load_time_s             FLOAT,
            fcp_ms                  FLOAT,
            lcp_ms                  FLOAT,
            ttfb_ms                 FLOAT,
            accessibility_issues    INTEGER,
            broken_links            INTEGER,
            js_errors               INTEGER,
            is_https                BOOLEAN,
            screenshot_path         VARCHAR(500)
        );
    """)
    conn.commit()
    print("  ✓ page_results table ready")
except Exception as e:
    conn.rollback()
    print(f"  ✗ page_results failed: {e}")

cur.close()
conn.close()
print("\nMigration complete. You can now restart the app.")