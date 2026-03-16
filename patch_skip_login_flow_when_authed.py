"""
patch_skip_login_flow_when_authed.py
Run from D:\\website_qa_tool: python patch_skip_login_flow_when_authed.py

What it fixes
-------------
When CRAWLER_USERNAME is set (authenticated crawl mode), the Login Flow
test case is pointless and always fails because:
  1. The test runner authenticates first
  2. It then tries to find the login form — which is now inaccessible
  3. All fill strategies fail on #txtUserName

Fix: skip generating a Login flow when crawler auth credentials are configured.
The crawler already proved login works by successfully authenticating.
"""
import shutil
from pathlib import Path

TARGET = Path("engines/flow_discovery.py")
if not TARGET.exists():
    print(f"ERROR: {TARGET} not found.")
    exit(1)

src = TARGET.read_text(encoding="utf-8")
shutil.copy(TARGET, TARGET.with_suffix(".py.bak"))

# In discover_flows (the main entry point), skip login flows when authenticated
old = '''    for page in pages_with_forms:
        flow = _build_form_flow(page, idx, flow_counter)
        if flow and flow.flow_type not in seen_form_types:
            flows.append(flow)
            seen_form_types.add(flow.flow_type)'''

new = '''    # When crawler auth is configured, skip generating a Login flow —
    # the crawler already proved login works, and an authenticated test
    # runner can't reach the login form anyway.
    _skip_login_flow = bool(os.environ.get("CRAWLER_USERNAME", "").strip())
    if _skip_login_flow:
        logger.info("[flow_discovery] CRAWLER_USERNAME set — skipping Login flow generation (auth already proven)")

    for page in pages_with_forms:
        flow = _build_form_flow(page, idx, flow_counter)
        if flow:
            if _skip_login_flow and flow.flow_type == "login":
                logger.debug(f"[flow_discovery] Skipping login flow for {page.get('url')}")
                continue
            if flow.flow_type not in seen_form_types:
                flows.append(flow)
                seen_form_types.add(flow.flow_type)'''

if old in src:
    src = src.replace(old, new, 1)
    TARGET.write_text(src, encoding="utf-8")
    print("  ✓ Login flow skipped when CRAWLER_USERNAME is configured")
    print("\nDone. Restart your RQ worker.")
    print("\nNext scan result:")
    print("  - No Login Flow test case generated")
    print("  - Only navigation flows remain → all should pass")
else:
    print("  ✗ Target not found — check file structure")
    if "skip generating a Login flow" in src:
        print("  Already patched.")