"""
fix_seo_db.py — One-time backfill for SEO data in existing page_results rows.

Fixes rows where:
  - seo_grade column is NULL  but seo_data JSONB contains 'seo_grade'
  - seo_score column is NULL  but seo_data JSONB contains 'seo_score'
  - seo_data JSONB is missing 'score' / 'grade' top-level keys

Run ONCE after deploying the SEO fix:
    python fix_seo_db.py

Safe to re-run — uses WHERE conditions so already-fixed rows are skipped.
"""

from dotenv import load_dotenv
load_dotenv()

import os, json
import psycopg2
from psycopg2.extras import RealDictCursor

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", 5432)),
    dbname=os.environ.get("DB_NAME", "qa_system"),
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASS", ""),
)
cur = conn.cursor(cursor_factory=RealDictCursor)

# ── 1. Backfill seo_grade column from JSONB where column is NULL ──────────────
print("Step 1: Backfilling seo_grade column from JSONB...")
cur.execute("""
    UPDATE page_results
    SET seo_grade = COALESCE(
        seo_data->>'grade',
        seo_data->>'seo_grade'
    )
    WHERE seo_grade IS NULL
      AND seo_data IS NOT NULL
      AND (seo_data ? 'grade' OR seo_data ? 'seo_grade')
""")
print(f"  ✓ Updated {cur.rowcount} rows — seo_grade column backfilled")
conn.commit()

# ── 2. Backfill seo_score column from JSONB where column is NULL ──────────────
print("Step 2: Backfilling seo_score column from JSONB...")
cur.execute("""
    UPDATE page_results
    SET seo_score = CAST(COALESCE(
        seo_data->>'score',
        seo_data->>'seo_score'
    ) AS FLOAT)
    WHERE seo_score IS NULL
      AND seo_data IS NOT NULL
      AND (seo_data ? 'score' OR seo_data ? 'seo_score')
      AND COALESCE(seo_data->>'score', seo_data->>'seo_score') ~ '^[0-9]+(\.[0-9]+)?$'
""")
print(f"  ✓ Updated {cur.rowcount} rows — seo_score column backfilled")
conn.commit()

# ── 3. Add 'score' + 'grade' top-level keys to JSONB where missing ────────────
print("Step 3: Patching seo_data JSONB to add score/grade top-level keys...")
cur.execute("""
    SELECT id, seo_data, seo_score, seo_grade
    FROM page_results
    WHERE seo_data IS NOT NULL
      AND (
          NOT (seo_data ? 'score')
          OR NOT (seo_data ? 'grade')
      )
""")
rows = cur.fetchall()
print(f"  Found {len(rows)} rows needing JSONB patch...")

patched = 0
for row in rows:
    seo_data = row['seo_data']
    if not isinstance(seo_data, dict):
        continue

    # Resolve score
    score = (
        seo_data.get('score') or
        seo_data.get('seo_score') or
        row['seo_score']
    )

    # Resolve grade — use string grades that match compute_seo_score output
    grade = seo_data.get('grade') or seo_data.get('seo_grade') or row['seo_grade']
    if grade is None and score is not None:
        s = float(score)
        if s >= 90:   grade = "Excellent"
        elif s >= 75: grade = "Good"
        elif s >= 50: grade = "Needs Improvement"
        else:         grade = "Poor"

    # Patch all four key variants into the JSONB
    updated = {
        **seo_data,
        "score":     score,
        "grade":     grade,
        "seo_score": score,
        "seo_grade": grade,
    }

    cur.execute(
        "UPDATE page_results SET seo_data = %s WHERE id = %s",
        (json.dumps(updated), row['id'])
    )
    patched += 1

conn.commit()
print(f"  ✓ Patched {patched} JSONB rows — score/grade keys added")

# ── 4. Verification report ────────────────────────────────────────────────────
print("\nVerification:")
cur.execute("""
    SELECT
        COUNT(*) FILTER (WHERE seo_data IS NOT NULL)                    AS total_with_seo,
        COUNT(*) FILTER (WHERE seo_grade IS NOT NULL)                   AS grade_col_filled,
        COUNT(*) FILTER (WHERE seo_score IS NOT NULL)                   AS score_col_filled,
        COUNT(*) FILTER (WHERE seo_data ? 'grade')                      AS jsonb_has_grade,
        COUNT(*) FILTER (WHERE seo_data ? 'score')                      AS jsonb_has_score,
        COUNT(*) FILTER (WHERE seo_grade IS NULL AND seo_data IS NOT NULL) AS still_null_grade
    FROM page_results
""")
stats = cur.fetchone()
for k, v in stats.items():
    print(f"  {k}: {v}")

cur.close()
conn.close()
print("\nDone. You can now reload the page — existing scan results will show SEO data.")