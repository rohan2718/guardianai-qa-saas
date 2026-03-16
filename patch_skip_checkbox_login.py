"""
patch_skip_checkbox_login.py
Run from D:\\website_qa_tool: python patch_skip_checkbox_login.py

What it fixes
-------------
Login Flow test includes a Step 2: "Enter checkbox: 'check'" for the
#chkUserType toggle. This toggle defaults to the correct position (ATIRA)
and should not be touched. Checkboxes/toggles in login forms are skipped.
"""
import shutil
from pathlib import Path

TARGET = Path("engines/flow_discovery.py")
if not TARGET.exists():
    print(f"ERROR: {TARGET} not found.")
    exit(1)

src = TARGET.read_text(encoding="utf-8")
shutil.copy(TARGET, TARGET.with_suffix(".py.bak"))

# The field-type filter currently skips: submit, button, reset, hidden, image
# Add checkbox and radio to that skip list for login flows
old = '''    # Fill each visible field with typed test values
    for f in (target_form.get("fields") or []):
        if f.get("type") in ("submit", "button", "reset", "hidden", "image"):
            continue
        if f.get("readonly") or f.get("disabled"):
            continue'''

new = '''    # Fill each visible field with typed test values
    for f in (target_form.get("fields") or []):
        if f.get("type") in ("submit", "button", "reset", "hidden", "image"):
            continue
        if f.get("readonly") or f.get("disabled"):
            continue
        # Skip checkboxes and radio buttons in login forms — they are toggles
        # (e.g. ATIRA/Customer switch) that must stay at their default position.
        if _is_login_flow and f.get("type") in ("checkbox", "radio"):
            continue'''

if old in src:
    src = src.replace(old, new, 1)
    TARGET.write_text(src, encoding="utf-8")
    print("  ✓ Checkboxes skipped in login flows")
    print("\nDone. Restart your RQ worker.")
else:
    print("  ✗ Target not found — checking for already-patched version")
    if "Skip checkboxes and radio buttons in login forms" in src:
        print("  Already patched.")
    else:
        print("  Could not patch automatically. Add this line manually:")
        print("  After 'if f.get(\"readonly\")...: continue'")
        print("  Add: if _is_login_flow and f.get(\"type\") in (\"checkbox\", \"radio\"): continue")