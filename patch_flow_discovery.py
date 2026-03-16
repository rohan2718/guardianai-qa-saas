"""
patch_flow_discovery.py
=======================
Run from D:\\website_qa_tool:  python patch_flow_discovery.py

What it fixes
-------------
  Login Flow test cases use "Test User Name" / "TestPassword123!" as fill values.
  This patch makes flow_discovery read CRAWLER_USERNAME and CRAWLER_PASSWORD
  from the environment (already set in .env) and use them when building Login flows.

  Before: Enter User Name: 'Test User Name'
  After:  Enter User Name: 'EMP002'
"""

import shutil
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

TARGET = Path("engines/flow_discovery.py")
if not TARGET.exists():
    print(f"ERROR: {TARGET} not found. Run from your project root.")
    exit(1)

src = TARGET.read_text(encoding="utf-8")
shutil.copy(TARGET, TARGET.with_suffix(".py.bak"))

# ── Fix 1: at the top of _build_form_flow, read auth credentials ─────────────
old_purpose_block = '''    purpose = target_form.get("form_purpose") or "Form"
    flow_type_map = {'''

new_purpose_block = '''    purpose = target_form.get("form_purpose") or "Form"

    # Read real credentials for login flows so the generated test actually passes.
    # Falls back to generic test values if not set.
    _auth_username = os.environ.get("CRAWLER_USERNAME", "").strip()
    _auth_password = os.environ.get("CRAWLER_PASSWORD", "").strip()
    _is_login_flow = purpose == "Login"

    flow_type_map = {'''

# ── Fix 2: swap generic values for real credentials on login fields ───────────
old_vals = '''        # Generate realistic test values
        if ftype == "email":
            test_val = "testuser@example.com"
        elif ftype == "password":
            test_val = "TestPassword123!"'''

new_vals = '''        # Generate realistic test values
        # For login forms, use real credentials from .env so the test actually passes
        if ftype == "email":
            test_val = "testuser@example.com"
        elif ftype == "password":
            test_val = _auth_password if (_is_login_flow and _auth_password) else "TestPassword123!"'''

# ── Fix 3: for text fields that look like username fields, use real username ──
old_text = '''        else:
            test_val = f"Test {label}"'''

new_text = '''        else:
            # If this looks like a username/login field in a login form, use real creds
            if _is_login_flow and _auth_username:
                label_lower = label.lower()
                if any(k in label_lower for k in ("user", "login", "username", "id", "employee", "emp")):
                    test_val = _auth_username
                else:
                    test_val = f"Test {label}"
            else:
                test_val = f"Test {label}"'''

changes = 0

if old_purpose_block in src:
    src = src.replace(old_purpose_block, new_purpose_block, 1)
    changes += 1
    print("  ✓ Added auth credential reading")
else:
    print("  ✗ Could not find purpose block — skipping Fix 1")

if old_vals in src:
    src = src.replace(old_vals, new_vals, 1)
    changes += 1
    print("  ✓ Password field uses CRAWLER_PASSWORD for login flows")
else:
    print("  ✗ Could not find password block — skipping Fix 2")

if old_text in src:
    src = src.replace(old_text, new_text, 1)
    changes += 1
    print("  ✓ Username/text field uses CRAWLER_USERNAME for login flows")
else:
    print("  ✗ Could not find text block — skipping Fix 3")

# ── Ensure os is imported ─────────────────────────────────────────────────────
if "import os" not in src:
    src = "import os\n" + src
    changes += 1
    print("  ✓ Added import os")

if changes > 0:
    TARGET.write_text(src, encoding="utf-8")
    print(f"\nDone — {changes} fix(es) applied. Backup at {TARGET.with_suffix('.py.bak')}")
    print("Restart your RQ worker to pick up the change.")
else:
    print("\nNothing patched — file may already be correct or structure differs.")