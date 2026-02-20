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

logger = logging.getLogger(__name__)

# Seconds to allow Cohere to respond before giving up and using basic summary
AI_SUMMARY_TIMEOUT = int(__import__("os").environ.get("AI_SUMMARY_TIMEOUT", 30))


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
        result = asyncio.run(
            run_crawler(
                run_id, url, user_id, page_limit,
                update_fn=update_progress,
                active_filters=active_filters,
            )
        )
    except Exception as e:
        logger.error(f"Crawler failed for run {run_id}: {e}", exc_info=True)
        with app.app_context():
            run = db.session.get(TestRun, run_id)
            if run:
                run.status      = "failed"
                run.finished_at = datetime.now(UTC)
                db.session.commit()
        return

    # ── Persist results ────────────────────────────────────────────────────────

    try:
        site_health    = result.get("site_health")    or {}
        component_avgs = site_health.get("component_averages") or {}
        score_dist     = site_health.get("score_distribution") or {}

        total_a11y    = 0
        total_broken  = 0
        total_js_err  = 0
        slow_pages    = 0

        raw_file = result.get("raw_file")
        pages: list = []
        if raw_file:
            try:
                import os
                if os.path.exists(raw_file):
                    with open(raw_file, "r", encoding="utf-8") as fh:
                        pages = json.load(fh)
            except Exception as e:
                logger.warning(f"Could not load raw file for run {run_id}: {e}")

        for p in pages:
            total_a11y   += p.get("accessibility_issues") or 0
            total_broken += len(p.get("broken_links") or [])
            total_js_err += len(p.get("js_errors")    or [])
            lt = p.get("load_time") or 0
            if lt > 3:
                slow_pages += 1

        with app.app_context():
            run = db.session.get(TestRun, run_id)
            if not run:
                logger.error(f"TestRun {run_id} disappeared before final persist")
                return

            run.status      = "completed"
            run.finished_at = datetime.now(UTC)
            run.total_tests = result.get("total", 0)
            run.passed      = result.get("passed", 0)
            run.failed      = result.get("failed", 0)
            run.scanned_pages = result.get("scanned_pages", 0)
            run.progress    = 100

            run.report_file       = result.get("report_file")
            run.summary_file      = result.get("summary_file")
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

            db.session.commit()
            run_id_final = run.id

        _persist_page_results(run_id_final, pages)

    except Exception as e:
        logger.error(f"Failed to persist results for run {run_id}: {e}", exc_info=True)
        with app.app_context():
            run = db.session.get(TestRun, run_id)
            if run:
                run.status      = "failed"
                run.finished_at = datetime.now(UTC)
                db.session.commit()


def _persist_page_results(run_id: int, pages: list):
    """
    Persists per-page PageResult records to DB.
    This is the primary data source for the paginated pages API.
    """
    if not pages:
        return

    with app.app_context():
        # Build failure_pattern lookup for similar_issue_ref (efficient raw query)
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
                    broken_links_count=len(p.get("broken_links") or []),
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