"""
diagnose_test_count.py
Run from project root: python diagnose_test_count.py

Loads the most recent completed scan's raw JSON and re-runs
flow_discovery + test_case_generator in isolation.
Shows exactly why test case count is low without triggering a full rescan.
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import app, db
from models import TestRun, PageResult

with app.app_context():
    run = (
        db.session.query(TestRun)
        .filter_by(status="completed")
        .order_by(TestRun.id.desc())
        .first()
    )
    if not run:
        print("No completed test runs found in database.")
        sys.exit(1)

    print(f"\n── Run ID: {run.id}  |  URL: {run.target_url}  |  Status: {run.status}")
    print(f"   Started: {run.started_at}  |  Pages scanned: {run.scanned_pages}\n")

    # ── Try raw JSON first (has full topology: connected_pages, nav_menus, forms) ──
    raw_path = None

    # Try by run ID first
    candidate = Path(f"raw/{run.id}.json")
    if candidate.exists():
        raw_path = candidate

    # Try run.raw_file path stored in DB
    if not raw_path and run.raw_file and Path(run.raw_file).exists():
        raw_path = Path(run.raw_file)

    # Fall back to most recently modified file in raw/
    if not raw_path:
        candidates = list(Path("raw").glob("*.json")) if Path("raw").exists() else []
        if candidates:
            raw_path = max(candidates, key=lambda p: p.stat().st_mtime)

    if raw_path and raw_path.exists():
        print(f"── Loading from raw JSON: {raw_path}")
        with open(raw_path, encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, list):
            page_data = raw
        elif isinstance(raw, dict):
            page_data = raw.get("pages") or raw.get("page_data") or []
        else:
            page_data = []

        print(f"── Pages loaded: {len(page_data)}")
        for p in page_data:
            cp  = len(p.get("connected_pages") or [])
            fm  = len(p.get("forms") or [])
            nav = len(p.get("nav_menus") or [])
            sb  = len(p.get("sidebar_links") or [])
            print(f"   {p.get('url','?')}  |  connected={cp}  forms={fm}  nav_menus={nav}  sidebar={sb}")

    else:
        print("── Raw JSON not found — using DB rows (topology will be empty)")
        db_rows = db.session.query(PageResult).filter_by(run_id=run.id).all()
        page_data = []
        for r in db_rows:
            page_data.append({
                "url":             r.url,
                "title":           r.title or "",
                "status":          r.status or 200,
                "forms":           [],
                "nav_menus":       [],
                "sidebar_links":   [],
                "connected_pages": [],
                "js_errors":       [],
                "broken_navigation_links": [],
            })
        print(f"── {len(page_data)} pages from DB (no topology — rescan needed for accurate results)")

    if not page_data:
        print("No page data found. Cannot diagnose.")
        sys.exit(1)

    # ── Run flow discovery ─────────────────────────────────────────────────────
    print(f"\n── Running flow discovery on {len(page_data)} pages ...")
    from engines.flow_discovery import discover_flows_as_dicts
    flows = discover_flows_as_dicts(page_data)
    print(f"   Flows discovered: {len(flows)}")
    for f in flows:
        print(f"   [{f['flow_type'].upper():14s}] {f['flow_id']}  —  {f['flow_name']}")

    # ── Run test case generation ───────────────────────────────────────────────
    print(f"\n── Running test case generation on {len(flows)} flows ...")
    from engines.test_case_generator import generate_test_cases_as_dicts
    test_cases = generate_test_cases_as_dicts(flows, run.id)
    print(f"   Test cases generated: {len(test_cases)}")
    for tc in test_cases:
        print(f"   [{tc['severity'].upper():8s}] {tc['tc_id']}  —  {tc['scenario']}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n── SUMMARY ─────────────────────────────────────────────────────────────")
    print(f"   Pages:      {len(page_data)}")
    print(f"   Flows:      {len(flows)}")
    print(f"   Test cases: {len(test_cases)}")

    if len(test_cases) < 5:
        print("\n   ⚠  Low count. Check the connected= values above.")
        print("      If most pages show connected=0, do a fresh rescan.")
        print("      The flow_discovery.py fixes only help if connected_pages is populated.")
    else:
        print("\n   ✓  Test case count looks healthy.")