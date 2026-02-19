"""
Background Task Worker — GuardianAI
Extended: ETA tracking, confidence score, AI fields, PageResult per-page DB persistence.
"""

import asyncio
import json
import logging
from datetime import datetime, UTC

from app import db, app
from models import TestRun, PageResult

logger = logging.getLogger(__name__)


def run_scan(run_id: int, url: str, page_limit, user_id: int, active_filters: list = None):
    """
    RQ-enqueued job. Runs full crawler with selected filters.
    Progress, ETA, and discovered_pages updated incrementally.
    All scores, confidence, and AI fields persisted to DB.
    """

    with app.app_context():
        run = TestRun.query.get(run_id)
        if not run:
            logger.error(f"TestRun {run_id} not found")
            return
        run.status = "running"
        run.started_at = datetime.now(UTC)
        if active_filters:
            import json as _json
            run.scan_filters = _json.dumps(active_filters)
        db.session.commit()

    # ── Real-time progress callback with ETA ──
    def update_progress(scanned: int, total: int, discovered: int = 0, avg_ms: float = None, eta_seconds: float = None):
        """Called after each page. Updates DB with live progress + ETA."""
        with app.app_context():
            run = TestRun.query.get(run_id)
            if run:
                run.scanned_pages = scanned
                run.discovered_pages = discovered
                run.progress = int((scanned / total) * 100) if total else 0
                if avg_ms is not None:
                    run.avg_scan_time_ms = avg_ms
                if eta_seconds is not None:
                    run.eta_seconds = eta_seconds
                db.session.commit()

    # ── Run crawl ──
    try:
        from crawler import main as run_crawler
        result = asyncio.run(
            run_crawler(
                run_id, url, user_id, page_limit,
                update_fn=update_progress,
                active_filters=active_filters
            )
        )
    except Exception as e:
        logger.error(f"Crawler failed for run {run_id}: {e}")
        with app.app_context():
            run = TestRun.query.get(run_id)
            if run:
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                db.session.commit()
        return

    # ── Persist results ──
    try:
        site_health = result.get("site_health") or {}
        component_avgs = site_health.get("component_averages") or {}
        score_dist = site_health.get("score_distribution") or {}

        total_a11y = 0
        total_broken = 0
        total_js_errors = 0
        slow_pages = 0

        raw_file = result.get("raw_file")
        pages = []
        if raw_file:
            try:
                with open(raw_file, "r", encoding="utf-8") as f:
                    pages = json.load(f)
                for p in pages:
                    total_a11y += p.get("accessibility_issues") or 0
                    total_broken += len(p.get("broken_links") or [])
                    total_js_errors += len(p.get("js_errors") or [])
                    load_time = p.get("load_time") or 0
                    if load_time > 3.0:
                        slow_pages += 1
            except Exception as e:
                logger.warning(f"Could not parse raw file: {e}")

        with app.app_context():
            run = TestRun.query.get(run_id)
            if not run:
                return

            run.status = "completed"
            run.finished_at = datetime.now(UTC)
            run.total_tests = result.get("total", 0)
            run.passed = result.get("passed", 0)
            run.failed = result.get("failed", 0)
            run.scanned_pages = result.get("scanned_pages", 0)
            run.progress = 100
            run.eta_seconds = 0.0

            run.report_file = result.get("report_file")
            run.summary_file = result.get("summary_file")
            run.raw_file = result.get("raw_file")
            run.site_summary_file = result.get("site_summary_file")

            # Site scores
            run.site_health_score = site_health.get("site_health_score")
            run.risk_category = site_health.get("risk_category")
            run.confidence_score = site_health.get("confidence_score") or result.get("confidence_score")

            # Component averages
            run.avg_performance_score = component_avgs.get("performance")
            run.avg_accessibility_score = component_avgs.get("accessibility")
            run.avg_security_score = component_avgs.get("security")
            run.avg_functional_score = component_avgs.get("functional")
            run.avg_ui_form_score = component_avgs.get("ui_form")

            # Aggregate counts
            run.total_accessibility_issues = total_a11y
            run.total_broken_links = total_broken
            run.total_js_errors = total_js_errors
            run.slow_pages_count = slow_pages

            # Risk distribution
            run.excellent_pages = score_dist.get("Excellent", 0)
            run.good_pages = score_dist.get("Good", 0)
            run.needs_attention_pages = score_dist.get("Needs Attention", 0)
            run.critical_pages = score_dist.get("Critical", 0)

            db.session.commit()
            run_id_final = run.id

        # ── Persist per-page PageResult records ──
        _persist_page_results(run_id_final, pages)

    except Exception as e:
        logger.error(f"Failed to persist results for run {run_id}: {e}")
        with app.app_context():
            run = TestRun.query.get(run_id)
            if run:
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                db.session.commit()


def _persist_page_results(run_id: int, pages: list):
    """
    Persists per-page PageResult records to DB.
    Includes confidence scores, AI learning fields, similar issue references.
    """
    if not pages:
        return

    with app.app_context():
        # Build failure_pattern lookup for similar_issue_ref
        existing_patterns = {}
        try:
            prior = (
                PageResult.query
                .filter(PageResult.failure_pattern_id.isnot(None))
                .filter(PageResult.run_id != run_id)
                .order_by(PageResult.id.desc())
                .limit(500)
                .all()
            )
            for pr in prior:
                if pr.failure_pattern_id and pr.failure_pattern_id not in existing_patterns:
                    existing_patterns[pr.failure_pattern_id] = pr.id
        except Exception as e:
            logger.warning(f"Could not load prior patterns: {e}")

        for p in pages:
            try:
                pattern_id = p.get("failure_pattern_id")
                similar_ref = existing_patterns.get(pattern_id) if pattern_id else None

                pr = PageResult(
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
                    similar_issue_ref=similar_ref,
                    ai_confidence=p.get("ai_confidence"),
                    self_healing_suggestion=p.get("self_healing_suggestion"),
                    load_time_s=p.get("load_time"),
                    fcp_ms=p.get("fcp_ms"),
                    lcp_ms=p.get("lcp_ms"),
                    ttfb_ms=p.get("ttfb_ms"),
                    accessibility_issues=p.get("accessibility_issues"),
                    broken_links=len(p.get("broken_links") or []),
                    js_errors=len(p.get("js_errors") or []),
                    is_https=p.get("is_https"),
                    screenshot_path=p.get("screenshot"),
                )
                db.session.add(pr)
            except Exception as e:
                logger.warning(f"Failed to persist PageResult for {p.get('url')}: {e}")
                continue

        try:
            db.session.commit()
            logger.info(f"Persisted {len(pages)} PageResult records for run {run_id}")
        except Exception as e:
            logger.error(f"PageResult commit failed for run {run_id}: {e}")
            db.session.rollback()