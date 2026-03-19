"""
migrate_db_deep_qa_patch.py
===========================
Adds the deep_qa_summary column to page_results.

Run ONCE: python migrate_db_deep_qa_patch.py
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

patches = [
    # (column_name, data_type, default_clause)
    ("deep_qa_summary", "JSONB", ""),
]

print("Patching page_results for Deep QA columns...")
for col, dtype, default in patches:
    try:
        sql = f"ALTER TABLE page_results ADD COLUMN {col} {dtype} {default};"
        cur.execute(sql)
        conn.commit()
        print(f"  ✓ Added: page_results.{col}")
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()
        print(f"  — Already exists: page_results.{col}")
    except Exception as e:
        conn.rollback()
        print(f"  ✗ Failed page_results.{col}: {e}")

cur.close()
conn.close()
print("\nDeep QA migration complete.")
print("\nAlso add to migrate_db.py page_result_patches list:")
print('    ("deep_qa_summary", "JSONB", ""),')