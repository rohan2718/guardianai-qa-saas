"""
tasks.py — GuardianAI Background Task Worker
RQ-enqueued scan job. Runs the crawler, persists all results to DB.
FIX: deprecated Session.query.get() → Session.get()
FIX: scan_filters stored/read as native JSONB (list), not JSON string
FIX: AI summary generation wrapped with timeout
"""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, UTC

from app import db, app
from models import TestRun, PageResult
from models_qa import QAFlow, QATestCase, QATestResult, BugReport
from engines.flow_discovery import discover_flows_as_dicts
from engines.test_case_generator import generate_test_cases_as_dicts
from engines.bug_reporter import generate_bugs_from_scan, generate_bugs_from_test_run
logger = logging.getLogger(__name__)

# Seconds to allow Cohere to respond before giving up and using basic summary
AI_SUMMARY_TIMEOUT = int(__import__("os").environ.get("AI_SUMMARY_TIMEOUT", 30))


def run_qa_pipeline(run_id: int, page_data: list, target_url: str = "") -> dict:
    """
    Full autonomous QA pipeline:
      Step 1 — Discover flows
      Step 2 — Generate test cases  [written as "pending" to DB]
      Step 3 — Execute test cases with Playwright
      Step 4 — Validate + update QATestCase status (pass/fail/skip)
      Step 5 — Bug reports from scan findings + test failures
      Step 6 — KPI scores from page data + test results
    """
    from engines.kpi_engine import compute_composite_kpis
    from engines.validation_engine import validate_test_case
    from sqlalchemy import select as _select

    summary = {
        "total_flows": 0, "total_test_cases": 0,
        "tests_passed": 0, "tests_failed": 0,
        "total_bugs": 0, "critical_bugs": 0,
        "high_bugs": 0, "medium_bugs": 0, "low_bugs": 0,
        "kpis": {},
    }

    try:
        # ── Step 1: Discover Flows ────────────────────────────────────────────
        flows = discover_flows_as_dicts(page_data)
        summary["total_flows"] = len(flows)
        logger.info(f"[qa_pipeline] run {run_id}: {len(flows)} flows discovered")

        flow_db_map = {}
        for flow in flows:
            db.session.add(QAFlow(
                run_id=run_id,
                flow_id=flow["flow_id"],
                flow_name=flow["flow_name"],
                flow_type=flow.get("flow_type"),
                priority=flow.get("priority"),
                entry_url=flow.get("entry_url"),
                exit_url=flow.get("exit_url"),
                description=flow.get("description"),
                tags=flow.get("tags", []),
                steps=flow.get("steps", []),
            ))
        db.session.flush()

        for db_flow in db.session.execute(
            _select(QAFlow).where(QAFlow.run_id == run_id)
        ).scalars().all():
            flow_db_map[db_flow.flow_id] = db_flow.id

        # ── Step 2: Generate Test Cases ───────────────────────────────────────
        test_cases = generate_test_cases_as_dicts(flows, run_id)
        summary["total_test_cases"] = len(test_cases)
        logger.info(f"[qa_pipeline] run {run_id}: {len(test_cases)} test cases generated")

        tc_db_ids = {}
        for tc in test_cases:
            db_tc = QATestCase(
                run_id=run_id,
                flow_id=tc.get("flow_id"),
                qa_flow_db_id=flow_db_map.get(tc.get("flow_id") or ""),
                tc_id=tc["tc_id"],
                scenario=tc["scenario"],
                description=tc.get("description"),
                preconditions=tc.get("preconditions", []),
                steps=tc.get("steps", []),
                expected_result=tc.get("expected_result"),
                status="pending",
                severity=tc.get("severity", "medium"),
                tags=tc.get("tags", []),
                playwright_snippet=tc.get("playwright_snippet"),
            )
            db.session.add(db_tc)
        db.session.flush()

        for db_tc in db.session.execute(
            _select(QATestCase).where(QATestCase.run_id == run_id)
        ).scalars().all():
            tc_db_ids[db_tc.tc_id] = db_tc.id

        # ── Step 3: Execute Test Cases ────────────────────────────────────────
        execution_results: list[dict] = []
        if test_cases and target_url:
            logger.info(f"[qa_pipeline] run {run_id}: executing {len(test_cases)} test cases")
            try:
                execution_results = _run_tests_sync(test_cases, run_id, target_url)
                summary["tests_passed"] = sum(1 for r in execution_results if r.get("status") == "pass")
                summary["tests_failed"] = sum(1 for r in execution_results if r.get("status") in ("fail", "error", "timeout"))
                logger.info(f"[qa_pipeline] run {run_id}: {summary['tests_passed']} passed / {summary['tests_failed']} failed")
            except Exception as exec_err:
                logger.error(f"[qa_pipeline] test execution error (non-fatal): {exec_err}", exc_info=True)
        else:
            if not target_url:
                logger.warning(f"[qa_pipeline] run {run_id}: no target_url — skipping test execution")

        # ── Step 4: Validate + update QATestCase rows ─────────────────────────
        validation_results: list[dict] = []
        result_map = {r["tc_id"]: r for r in execution_results}

        for tc in test_cases:
            tc_id  = tc["tc_id"]
            er     = result_map.get(tc_id, {})
            status = er.get("status", "pending")

            try:
                vr = validate_test_case(tc_id, tc.get("expected_result", ""), er)
                validation_results.append(vr.to_dict() if hasattr(vr, "to_dict") else vars(vr))
            except Exception:
                validation_results.append({"tc_id": tc_id, "verdict": "inconclusive"})

            db_status = {"pass": "pass", "fail": "fail", "error": "fail",
                         "timeout": "fail", "skip": "skip"}.get(status, "pending")

            db_tc_id = tc_db_ids.get(tc_id)
            if db_tc_id:
                db_tc_obj = db.session.get(QATestCase, db_tc_id)
                if db_tc_obj:
                    db_tc_obj.status        = db_status
                    db_tc_obj.actual_result = er.get("actual_result")

            if er:
                db.session.add(QATestResult(
                    run_id=run_id,
                    test_case_id=tc_db_ids.get(tc_id),
                    tc_id=tc_id,
                    flow_id=tc.get("flow_id"),
                    scenario=tc.get("scenario"),
                    status=db_status,
                    actual_result=er.get("actual_result"),
                    failure_step=er.get("failure_step"),
                    failure_reason=er.get("failure_reason"),
                    duration_ms=er.get("duration_ms"),
                    screenshot_path=er.get("screenshot_path"),
                    step_results=er.get("step_results", []),
                ))

        # ── Step 5: Bugs — scan findings + test failures ──────────────────────
        all_bugs = list(generate_bugs_from_scan(page_data, run_id))

        if execution_results:
            try:
                test_bugs = generate_bugs_from_test_run(
                    test_cases, execution_results, validation_results, run_id
                )
                all_bugs.extend(test_bugs)
            except Exception as tb_err:
                logger.warning(f"[qa_pipeline] test bug generation error (non-fatal): {tb_err}")

        for bug in all_bugs:
            summary["total_bugs"] += 1
            sev_key = f"{bug.severity}_bugs"
            summary[sev_key] = summary.get(sev_key, 0) + 1
            db.session.add(BugReport(
                run_id=run_id,
                bug_title=bug.bug_title,
                page_url=bug.page_url,
                bug_type=bug.bug_type,
                severity=bug.severity,
                component=bug.component,
                description=bug.description,
                impact=bug.impact,
                steps_to_reproduce=bug.steps_to_reproduce,
                expected_result=bug.expected_result,
                actual_result=bug.actual_result,
                suggested_fix=bug.suggested_fix,
                screenshot_path=bug.screenshot_path,
                source=getattr(bug, "source", "scan"),
                tc_id=getattr(bug, "tc_id", None),
                flow_id=getattr(bug, "flow_id", None),
                playwright_snippet=getattr(bug, "playwright_snippet", None),
            ))

        # ── Step 6: KPI Scores ────────────────────────────────────────────────
        kpis = compute_composite_kpis(page_data, execution_results or None)
        summary["kpis"] = kpis
        logger.info(f"[qa_pipeline] run {run_id}: KPIs computed — health={kpis.get('site_health_score')}")

        db.session.commit()
        logger.info(
            f"[qa_pipeline] run {run_id}: flows={summary['total_flows']} "
            f"tests={summary['total_test_cases']} bugs={summary['total_bugs']} "
            f"(critical={summary.get('critical_bugs',0)}, high={summary.get('high_bugs',0)})"
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"[qa_pipeline] failed for run {run_id}: {e}", exc_info=True)

    return summary


def _run_tests_sync(test_cases: list[dict], run_id: int, target_url: str) -> list[dict]:
    """
    Runs the async test runner in a dedicated event loop (safe for RQ workers).
    Returns a list of execution result dicts.
    """
    import asyncio
    from playwright.async_api import async_playwright
    from engines.test_runner import run_all_test_cases

    async def _execute():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=True,
            )

            try:
                from crawler import _read_auth_config, _do_login
                auth = _read_auth_config()
                if auth:
                    login_ok = await _do_login(context, auth)
                    if login_ok:
                        logger.info("[test_runner_auth] Session established for test execution")
                    else:
                        logger.warning("[test_runner_auth] Login failed — tests will run unauthenticated")
            except Exception as auth_err:
                logger.warning(f"[test_runner_auth] Auth setup error (non-fatal): {auth_err}")

            try:
                results = await run_all_test_cases(context, test_cases, run_id)
                return [
                    r.to_dict() if hasattr(r, "to_dict") else vars(r)
                    for r in results
                ]
            finally:
                await browser.close()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_execute())
    finally:
        loop.close()


def _persist_deep_qa_results(run_id: int, results: list) -> None:
    """
    Persist DeepQA results to DB:
      - Each page result → stored in PageResult.ui_summary JSONB under "deep_qa" key
      - Each FAIL → BugReport row (source="deep_qa")
      - TestRun aggregate totals updated
    """
    if not results:
        return

    total_buttons_tested = 0
    total_buttons_failed = 0
    total_forms_tested   = 0
    total_forms_failed   = 0
    total_links_broken   = 0
    deep_qa_scores       = []

    with app.app_context():
        try:
            for page_result in results:
                try:
                    data = page_result.to_dict() if hasattr(page_result, "to_dict") else page_result

                    summary = data.get("summary", {})
                    total_buttons_tested += summary.get("total_buttons", 0)
                    total_buttons_failed += summary.get("buttons_failed", 0)
                    total_forms_tested   += summary.get("total_forms", 0)
                    total_forms_failed   += summary.get("forms_failed", 0)
                    total_links_broken   += summary.get("links_broken", 0)
                    qa_score              = data.get("qa_score", 100.0)
                    if qa_score is not None:
                        deep_qa_scores.append(qa_score)

                    # ── Store in PageResult.ui_summary JSONB ──────────────────
                    pr = PageResult.query.filter_by(
                        run_id=run_id,
                        url=data.get("page_url"),
                    ).first()
                    if pr:
                        # CRITICAL: create a NEW dict (not in-place mutation).
                        # SQLAlchemy does not detect in-place JSONB mutations.
                        # Assigning the same dict object back silently skips the UPDATE.
                        from sqlalchemy.orm.attributes import flag_modified
                        new_summary = dict(pr.ui_summary or {})
                        new_summary["deep_qa"] = {
                            "qa_score":          qa_score,
                            "summary":           summary,
                            "buttons":           data.get("buttons", []),
                            "forms":             data.get("forms", []),
                            "links":             [l for l in data.get("links", []) if l.get("status") == "FAIL"][:20],
                            "tables":            data.get("tables", []),
                            "modals":            data.get("modals", []),
                            "js_errors_on_load": data.get("js_errors_on_load", []),
                            "broken_images":     data.get("broken_images", []),
                            "performance":       data.get("performance", {}),
                        }
                        pr.ui_summary = new_summary
                        flag_modified(pr, "ui_summary")  # force SQLAlchemy to include in UPDATE

                    # ── Create BugReport for each failure ─────────────────────
                    for bug in data.get("bugs", []):
                        try:
                            db.session.add(BugReport(
                                run_id=run_id,
                                bug_title=bug.get("title", "DeepQA Issue"),
                                page_url=data.get("page_url"),
                                bug_type=bug.get("bug_type", "functional"),
                                severity=bug.get("severity", "medium"),
                                description=bug.get("description", ""),
                                impact=bug.get("impact", ""),
                                steps_to_reproduce=bug.get("steps", []),
                                expected_result=bug.get("expected", ""),
                                actual_result=bug.get("actual", ""),
                                suggested_fix=bug.get("fix", ""),
                                screenshot_path=bug.get("screenshot_path"),
                                source="deep_qa",
                            ))
                        except Exception as bug_err:
                            logger.warning(f"[DeepQA persist] Bug insert error: {bug_err}")

                except Exception as page_err:
                    logger.warning(f"[DeepQA persist] Page result error: {page_err}")

            # ── Update TestRun aggregates ──────────────────────────────────────
            try:
                run_obj = db.session.get(TestRun, run_id)
                if run_obj:
                    existing_bugs = run_obj.total_bugs or 0
                    new_bugs = sum(
                        len(r.to_dict().get("bugs", [])) if hasattr(r, "to_dict") else 0
                        for r in results
                    )
                    if hasattr(run_obj, "total_bugs"):
                        run_obj.total_bugs = existing_bugs + new_bugs

                    if deep_qa_scores:
                        avg_dqa = round(sum(deep_qa_scores) / len(deep_qa_scores), 1)
                        logger.info(f"[DeepQA] Avg QA score for run {run_id}: {avg_dqa}")
            except Exception as run_err:
                logger.warning(f"[DeepQA persist] TestRun update error: {run_err}")

            db.session.commit()
            logger.info(
                f"[DeepQA persist] Run {run_id}: "
                f"buttons_tested={total_buttons_tested} failed={total_buttons_failed} "
                f"forms_tested={total_forms_tested} failed={total_forms_failed} "
                f"links_broken={total_links_broken}"
            )

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.error(f"[DeepQA persist] Fatal error for run {run_id}: {e}", exc_info=True)


def run_scan(run_id: int, url: str, page_limit, user_id: int, active_filters: list = None):
    """
    RQ-enqueued job. Progress, ETA, and discovered_pages updated incrementally.
    All scores, confidence, and AI fields persisted to DB.
    """

    with app.app_context():
        run = db.session.get(TestRun, run_id)
        if not run:
            logger.error(f"TestRun {run_id} not found")
            return
        run.status     = "running"
        run.started_at = datetime.now(UTC)
        # scan_filters is now JSONB — store as list directly
        if active_filters:
            run.scan_filters = active_filters
        db.session.commit()

    # ── Real-time progress callback ────────────────────────────────────────────

    def update_progress(
        scanned: int,
        total: int,
        discovered: int = 0,
        avg_ms: float = None,
        eta_seconds: float = None,
    ):
        with app.app_context():
            run = db.session.get(TestRun, run_id)
            if run:
                run.scanned_pages    = scanned
                run.discovered_pages = discovered
                run.progress         = int((scanned / total) * 100) if total else 0
                if avg_ms is not None:
                    run.avg_scan_time_ms = avg_ms
                if eta_seconds is not None:
                    run.eta_seconds = eta_seconds
                db.session.commit()

    # ── Run crawl ─────────────────────────────────────────────────────────────

    try:
        from crawler import main as run_crawler
        from app import _release_scan_slot

        try:
            result = asyncio.run(
                run_crawler(
                    run_id, url, user_id, page_limit,
                    update_fn=update_progress,
                    active_filters=active_filters,
                )
            )
        finally:
            _release_scan_slot()

    except Exception as e:
        logger.error(f"Crawler raised unexpectedly for run {run_id}: {e}", exc_info=True)
        with app.app_context():
            run = db.session.get(TestRun, run_id)
            if run:
                run.status       = "failed"
                run.error_detail = f"Crawler exception: {str(e)[:500]}"
                run.finished_at  = datetime.now(UTC)
                db.session.commit()
        return

    # ── Handle pre-flight failure returned from crawler ───────────────────────
    if result.get("status") == "target_unreachable":
        error_msg = result.get("error_detail", "Target server could not be reached.")
        logger.warning(f"[run {run_id}] Marking as target_unreachable: {error_msg}")
        with app.app_context():
            run = db.session.get(TestRun, run_id)
            if run:
                run.status       = "target_unreachable"
                run.error_detail = error_msg
                run.finished_at  = datetime.now(UTC)
                run.confidence   = 0.0
                db.session.commit()
        return

    # ── Persist results ────────────────────────────────────────────────────────
    _deep_qa_page_urls = []  # populated inside context, used outside

    try:
        import os as _os
        import markdown as _markdown

        site_health    = result.get("site_health")    or {}
        component_avgs = site_health.get("component_averages") or {}
        score_dist     = site_health.get("score_distribution") or {}

        # ── Accumulate aggregate counters from raw page data ─────────────────
        total_a11y   = 0
        total_broken = 0
        total_js_err = 0
        slow_pages   = 0

        raw_file = result.get("raw_file")
        pages: list = []
        if raw_file:
            try:
                if _os.path.exists(raw_file):
                    with open(raw_file, "r", encoding="utf-8") as fh:
                        pages = json.load(fh)
            except Exception as e:
                logger.warning(f"Could not load raw file for run {run_id}: {e}")

        for p in pages:
            total_a11y   += p.get("accessibility_issues") or 0
            total_broken += len(p.get("broken_navigation_links") or [])
            total_js_err += len(p.get("js_errors") or [])
            lt = p.get("load_time") or 0
            if lt > 3:
                slow_pages += 1

        # ── Read AI summary text from file ───────────────────────────────────
        ai_summary_text = None
        ai_summary_html = None
        summary_file_path = result.get("summary_file")
        if summary_file_path and _os.path.exists(summary_file_path):
            try:
                with open(summary_file_path, "r", encoding="utf-8") as fh:
                    ai_summary_text = fh.read()
                if ai_summary_text:
                    ai_summary_html = _markdown.markdown(ai_summary_text)
            except Exception as e:
                logger.warning(f"Could not read AI summary file for run {run_id}: {e}")

        # ── ATOMIC COMMIT: TestRun + PageResult in one transaction ────────────
        with app.app_context():
            run = db.session.get(TestRun, run_id)
            if not run:
                logger.error(f"TestRun {run_id} disappeared before final persist")
                return

            # Populate aggregate fields
            run.total_tests   = result.get("total", 0)
            run.passed        = result.get("passed", 0)
            run.failed        = result.get("failed", 0)
            run.scanned_pages = result.get("scanned_pages", 0)
            run.progress      = 100

            run.report_file       = result.get("report_file")
            run.summary_file      = summary_file_path
            run.raw_file          = result.get("raw_file")
            run.site_summary_file = result.get("site_summary_file")

            run.site_health_score = site_health.get("site_health_score")
            run.risk_category     = site_health.get("risk_category")
            run.confidence_score  = result.get("confidence_score")

            run.avg_performance_score   = component_avgs.get("performance")
            run.avg_accessibility_score = component_avgs.get("accessibility")
            run.avg_security_score      = component_avgs.get("security")
            run.avg_functional_score    = component_avgs.get("functional")
            run.avg_ui_form_score       = component_avgs.get("ui_form")

            run.total_accessibility_issues = total_a11y
            run.total_broken_links         = total_broken
            run.total_js_errors            = total_js_err
            run.slow_pages_count           = slow_pages

            run.excellent_pages       = score_dist.get("Excellent",       0)
            run.good_pages            = score_dist.get("Good",            0)
            run.needs_attention_pages = score_dist.get("Needs Attention", 0)
            run.critical_pages        = score_dist.get("Critical",        0)

            run.ai_summary      = ai_summary_text
            run.ai_summary_html = ai_summary_html

            # Build PageResult objects in-session before any commit
            try:
                existing_patterns: dict = {}
                from sqlalchemy import text as _text
                rows = db.session.execute(
                    _text(
                        "SELECT DISTINCT ON (failure_pattern_id) failure_pattern_id, id "
                        "FROM page_results "
                        "WHERE run_id != :run_id AND failure_pattern_id IS NOT NULL "
                        "ORDER BY failure_pattern_id, id DESC "
                        "LIMIT 500"
                    ),
                    {"run_id": run_id},
                ).fetchall()
                existing_patterns = {row[0]: row[1] for row in rows}
            except Exception as e:
                logger.warning(f"Could not load prior patterns: {e}")
                existing_patterns = {}

            page_records = []
            for p in pages:
                try:
                    pattern_id  = p.get("failure_pattern_id")
                    similar_ref = existing_patterns.get(pattern_id) if pattern_id else None
                    page_records.append(PageResult(
                        run_id=run_id,
                        url=p.get("url"),
                        title=p.get("title"),
                        scanned_at=datetime.now(UTC),
                        status=p.get("status"),
                        health_score=p.get("health_score"),
                        risk_category=p.get("risk_category"),
                        performance_score=p.get("performance_score"),
                        accessibility_score=p.get("accessibility_score"),
                        security_score=p.get("security_score"),
                        functional_score=p.get("functional_score"),
                        ui_form_score=p.get("ui_form_score"),
                        confidence_score=p.get("confidence_score"),
                        checks_executed=p.get("checks_executed"),
                        checks_null=p.get("checks_null"),
                        failure_pattern_id=pattern_id,
                        root_cause_tag=p.get("root_cause_tag"),
                        self_healing_suggestion=p.get("self_healing_suggestion"),
                        similar_issue_ref=similar_ref,
                        load_time=p.get("load_time"),
                        fcp_ms=p.get("fcp_ms"),
                        lcp_ms=p.get("lcp_ms"),
                        ttfb_ms=p.get("ttfb_ms"),
                        accessibility_issues=p.get("accessibility_issues"),
                        broken_links_count=len(p.get("broken_navigation_links") or []),
                        js_errors_count=len(p.get("js_errors") or []),
                        is_https=p.get("is_https"),
                        screenshot_path=p.get("screenshot"),
                        ui_summary=p.get("ui_summary"),
                    ))
                except Exception as e:
                    logger.warning(f"Skipping page record for {p.get('url')}: {e}")

            # Add all page records to session (no intermediate commit)
            if page_records:
                db.session.bulk_save_objects(page_records)

            try:
                qa_summary = run_qa_pipeline(run_id, pages, target_url=url)
                kpis = qa_summary.get("kpis", {})

                run.avg_performance_score   = kpis.get("avg_performance_score")
                run.avg_accessibility_score = kpis.get("avg_accessibility_score")
                run.avg_security_score      = kpis.get("avg_security_score")
                run.avg_functional_score    = kpis.get("avg_functional_score")
                run.avg_ui_form_score       = kpis.get("avg_ui_form_score")

                if kpis.get("site_health_score") is not None:
                    run.site_health_score = kpis["site_health_score"]
                if kpis.get("risk_category"):
                    run.risk_category = kpis["risk_category"]

                run.slow_pages_count           = kpis.get("slow_pages_count", 0)
                run.total_broken_links         = kpis.get("total_broken_links", 0)
                run.total_js_errors            = kpis.get("total_js_errors", 0)
                run.total_accessibility_issues = kpis.get("total_accessibility_issues", 0)

                if hasattr(run, "total_bugs"):
                    run.total_bugs    = qa_summary.get("total_bugs", 0)
                    run.critical_bugs = qa_summary.get("critical_bugs", 0)
                    run.high_bugs     = qa_summary.get("high_bugs", 0)
                    run.medium_bugs   = qa_summary.get("medium_bugs", 0)
                    run.low_bugs      = qa_summary.get("low_bugs", 0)
                if hasattr(run, "total_flows"):
                    run.total_flows      = qa_summary.get("total_flows", 0)
                if hasattr(run, "total_test_cases"):
                    run.total_test_cases = qa_summary.get("total_test_cases", 0)
                if hasattr(run, "tests_passed"):
                    run.tests_passed = qa_summary.get("tests_passed", 0)
                    run.tests_failed = qa_summary.get("tests_failed", 0)
                if hasattr(run, "qa_enabled"):
                    run.qa_enabled = True
            except Exception as qa_err:
                logger.error(f"QA pipeline error (non-fatal): {qa_err}", exc_info=True)

            # ── COMMIT completed IMMEDIATELY — UI reloads, user sees results ──
            # DeepQA runs AFTER in its own isolated context (no nested session bug)
            try:
                run.status      = "completed"
                run.finished_at = datetime.now(UTC)
                db.session.commit()
                logger.info(f"[run {run_id}] Committed: {len(page_records)} PageResults, status=completed")
            except Exception as commit_err:
                db.session.rollback()
                logger.error(f"[run {run_id}] Atomic commit failed: {commit_err}", exc_info=True)
                run2 = db.session.get(TestRun, run_id)
                if run2:
                    run2.status      = "failed"
                    run2.finished_at = datetime.now(UTC)
                    db.session.commit()

            # Capture URLs before the outer context closes
            _deep_qa_page_urls = [p["url"] for p in pages[:20] if p.get("url")]

    except Exception as e:
        logger.error(f"Failed to persist results for run {run_id}: {e}", exc_info=True)
        with app.app_context():
            run = db.session.get(TestRun, run_id)
            if run:
                run.status      = "failed"
                run.finished_at = datetime.now(UTC)
                db.session.commit()
        return  # Don't run DeepQA if main scan failed

    # ══════════════════════════════════════════════════════════════════════════
    # DEEP QA — completely outside the main try/except, in its own fresh context.
    # _persist_deep_qa_results has its OWN with app.app_context() — no nesting.
    # This eliminates the SQLAlchemy session invalidation bug entirely.
    # ══════════════════════════════════════════════════════════════════════════
    try:
        from engines.deep_qa_engine import run_deep_qa
        from playwright.async_api import async_playwright as _async_playwright

        async def _run_deep_qa_session():
            async with _async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    ignore_https_errors=True,
                )
                try:
                    from crawler import _read_auth_config, _do_login
                    auth = _read_auth_config()
                    if auth:
                        login_ok = await _do_login(context, auth)
                        if login_ok:
                            logger.info("[DeepQA] Session authenticated for deep testing")
                        else:
                            logger.warning("[DeepQA] Login failed — testing unauthenticated")
                except Exception as auth_err:
                    logger.warning(f"[DeepQA] Auth setup error (non-fatal): {auth_err}")

                deep_results = await run_deep_qa(context, _deep_qa_page_urls, run_id)
                await browser.close()
                return deep_results

        logger.info(f"[DeepQA] Starting deep QA for run {run_id} ({len(_deep_qa_page_urls)} pages)")
        deep_qa_results = asyncio.run(_run_deep_qa_session())
        _persist_deep_qa_results(run_id, deep_qa_results)
        logger.info(f"[DeepQA] Complete for run {run_id}: {len(deep_qa_results)} pages deep-tested")

    except Exception as deep_qa_err:
        logger.error(f"[DeepQA] Pipeline failed (non-fatal): {deep_qa_err}", exc_info=True)


def _persist_page_results(run_id: int, pages: list):
    """
    Persists per-page PageResult records to DB.
    This is the primary data source for the paginated pages API.
    """
    if not pages:
        return

    with app.app_context():
        existing_patterns: dict = {}
        try:
            from sqlalchemy import text
            rows = db.session.execute(
                text(
                    "SELECT DISTINCT ON (failure_pattern_id) failure_pattern_id, id "
                    "FROM page_results "
                    "WHERE run_id != :run_id AND failure_pattern_id IS NOT NULL "
                    "ORDER BY failure_pattern_id, id DESC "
                    "LIMIT 500"
                ),
                {"run_id": run_id},
            ).fetchall()
            existing_patterns = {row[0]: row[1] for row in rows}
        except Exception as e:
            logger.warning(f"Could not load prior patterns: {e}")

        records = []
        for p in pages:
            try:
                pattern_id  = p.get("failure_pattern_id")
                similar_ref = existing_patterns.get(pattern_id) if pattern_id else None

                records.append(PageResult(
                    run_id=run_id,
                    url=p.get("url"),
                    title=p.get("title"),
                    scanned_at=datetime.now(UTC),
                    status=p.get("status"),
                    health_score=p.get("health_score"),
                    risk_category=p.get("risk_category"),
                    performance_score=p.get("performance_score"),
                    accessibility_score=p.get("accessibility_score"),
                    security_score=p.get("security_score"),
                    functional_score=p.get("functional_score"),
                    ui_form_score=p.get("ui_form_score"),
                    confidence_score=p.get("confidence_score"),
                    checks_executed=p.get("checks_executed"),
                    checks_null=p.get("checks_null"),
                    failure_pattern_id=pattern_id,
                    root_cause_tag=p.get("root_cause_tag"),
                    self_healing_suggestion=p.get("self_healing_suggestion"),
                    similar_issue_ref=similar_ref,
                    load_time=p.get("load_time"),
                    fcp_ms=p.get("fcp_ms"),
                    lcp_ms=p.get("lcp_ms"),
                    ttfb_ms=p.get("ttfb_ms"),
                    accessibility_issues=p.get("accessibility_issues"),
                    broken_links_count=len(p.get("broken_navigation_links") or []),
                    js_errors_count=len(p.get("js_errors") or []),
                    is_https=p.get("is_https"),
                    screenshot_path=p.get("screenshot"),
                    ui_summary=p.get("ui_summary"),
                ))
            except Exception as e:
                logger.warning(f"Skipping page result for {p.get('url')}: {e}")

        try:
            db.session.bulk_save_objects(records)
            db.session.commit()
            logger.info(f"Persisted {len(records)} PageResult records for run {run_id}")
        except Exception as e:
            db.session.rollback()
            logger.error(f"Bulk save failed for run {run_id}: {e}", exc_info=True)


def _run_ai_summary_with_timeout(page_data: list, timeout: int = AI_SUMMARY_TIMEOUT) -> str:
    """
    Runs AI summary generation with a hard timeout.
    Falls back to basic_summary() if Cohere is slow or unavailable.
    """
    from ai_analyzer import analyze_site, basic_summary

    def _call():
        return analyze_site(page_data)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_call)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout:
            logger.warning(f"AI summary timed out after {timeout}s — using basic summary")
            return basic_summary(page_data)
        except Exception as e:
            logger.warning(f"AI summary failed: {e} — using basic summary")
            return basic_summary(page_data)