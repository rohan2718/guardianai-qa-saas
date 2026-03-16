"""
patch_tasks.py
==============
Run this from your project root to fix the ImportError in tasks.py:

    python patch_tasks.py

What it fixes
-------------
  validate_test_result  →  validate_test_case   (wrong name → correct name)

That is the only change. Everything else in tasks.py is left untouched.
"""

import re, shutil, sys
from pathlib import Path

TARGET = Path("tasks.py")

if not TARGET.exists():
    print(f"ERROR: {TARGET} not found. Run this from your project root.")
    sys.exit(1)

src = TARGET.read_text(encoding="utf-8")

FIXES = [
    (
        "from engines.validation_engine import validate_test_result",
        "from engines.validation_engine import validate_test_case",
    ),
    (
        "vr = validate_test_result(",
        "vr = validate_test_case(",
    ),
]

changed = 0
for old, new in FIXES:
    if old in src:
        src = src.replace(old, new)
        changed += 1
        print(f"  FIXED: {old!r}")
    else:
        print(f"  SKIP (not found): {old!r}")

if changed:
    shutil.copy(TARGET, TARGET.with_suffix(".py.bak"))
    TARGET.write_text(src, encoding="utf-8")
    print(f"\nDone — {changed} replacement(s) applied. Backup saved as tasks.py.bak")
    print("Restart your RQ worker to pick up the change.")
else:
    print("\nNothing to patch — file may already be correct.")