"""
analytics.py — GuardianAI
Dashboard metrics derived from DB aggregate fields stored on TestRun.
No Excel file reads, no Python-side aggregation loops.

generate_metrics_from_run(run) → primary function for dashboard display.
generate_metrics(records)      → kept for legacy export/Excel compatibility only.
"""

from collections import Counter
from models import db  # shared SQLAlchemy instance


# ── Primary: DB-backed metrics (fast, no file I/O) ──────────────────────────

def generate_metrics_from_run(run) -> dict:
    """
    Produces display metrics directly from TestRun aggregate columns.
    All values were computed and stored when the scan completed.
    Zero file reads. Zero Python aggregation.

    Args:
        run: TestRun model instance (must be fully persisted / status=completed).

    Returns:
        Metrics dict compatible with the frontend template context.
    """
    if run is None:
        return {}

    total = run.total_tests or 0
    passed = run.passed or 0
    failed = run.failed or 0

    # avg_load_time — aggregate from PageResult records (not stored at run level)
    from models import PageResult
    from sqlalchemy import func as sqlfunc
    avg_load = db.session.query(
        sqlfunc.avg(PageResult.load_time)
    ).filter(
        PageResult.run_id == run.id,
        PageResult.load_time.isnot(None)
    ).scalar()
    avg_load = round(avg_load, 2) if avg_load is not None else None
    slow_pages = run.slow_pages_count or 0

    return {
        "total_pages":        total,
        "passed":             passed,
        "failed":             failed,
        "active_pages":       passed,          # alias used by index.html template
        "not_found_pages":    failed,          # alias used by index.html template
        "pass_rate":          round((passed / total * 100), 1) if total else 0,
        "avg_load_time":      avg_load,
        "slow_pages":         slow_pages,
        "avg_health":         round(run.site_health_score or 0, 1),
        "avg_performance":    round(run.avg_performance_score   or 0, 1),
        "avg_accessibility":  round(run.avg_accessibility_score or 0, 1),
        "avg_security":       round(run.avg_security_score      or 0, 1),
        "avg_functional":     round(run.avg_functional_score    or 0, 1),
        "avg_ui_form":        round(run.avg_ui_form_score       or 0, 1),
        "confidence_score":   round(run.confidence_score        or 0, 1),
        "total_a11y_issues":  run.total_accessibility_issues or 0,
        "total_broken_links": run.total_broken_links          or 0,
        "total_js_errors":    run.total_js_errors             or 0,
        "risk_category":      run.risk_category,
        "score_distribution": {
            "Excellent":       run.excellent_pages       or 0,
            "Good":            run.good_pages            or 0,
            "Needs Attention": run.needs_attention_pages or 0,
            "Critical":        run.critical_pages        or 0,
        },
    }


# ── Legacy: Excel-record-based aggregation (export compatibility only) ────────

def generate_metrics(records: list) -> dict:
    """
    Legacy function: takes report rows (list of dicts from Excel/DataFrame)
    and returns summary metrics. Still used when loading the Excel export.
    Do NOT use this for the live dashboard — use generate_metrics_from_run().
    """
    if not records:
        return {}

    statuses       = []
    load_times     = []
    health_scores  = []
    perf_scores    = []
    a11y_scores    = []
    sec_scores     = []
    a11y_issues    = []
    broken_links   = []
    js_errors      = []

    for r in records:
        try:
            statuses.append(int(r.get("Status", 0)))
        except (TypeError, ValueError):
            statuses.append(0)

        try:
            val = r.get("Load Time (s)", r.get("Load Time", 0))
            load_times.append(float(val))
        except (TypeError, ValueError):
            load_times.append(0.0)

        for key, target in [
            ("Health Score",        health_scores),
            ("Performance Score",   perf_scores),
            ("Accessibility Score", a11y_scores),
            ("Security Score",      sec_scores),
        ]:
            try:
                v = r.get(key)
                if v not in (None, "", "None"):
                    target.append(float(v))
            except (TypeError, ValueError):
                pass

        for key, target in [
            ("Accessibility Issues", a11y_issues),
            ("Broken Links",         broken_links),
            ("JS Errors",            js_errors),
        ]:
            try:
                v = r.get(key, 0)
                target.append(int(float(v)) if v not in (None, "", "None") else 0)
            except (TypeError, ValueError):
                target.append(0)

    counter = Counter(statuses)
    total   = len(records)
    passed  = counter.get(200, 0)
    failed  = total - passed
    slow    = sum(1 for t in load_times if t > 3)

    def _avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else None

    return {
        "total_pages":        total,
        "passed":             passed,
        "failed":             failed,
        "pass_rate":          round(passed / total * 100, 1) if total else 0,
        "avg_load_time":      _avg(load_times),
        "slow_pages":         slow,
        "avg_health":         _avg(health_scores),
        "avg_performance":    _avg(perf_scores),
        "avg_accessibility":  _avg(a11y_scores),
        "avg_security":       _avg(sec_scores),
        "total_a11y_issues":  sum(a11y_issues),
        "total_broken_links": sum(broken_links),
        "total_js_errors":    sum(js_errors),
        "status_counts":      dict(counter),
    }