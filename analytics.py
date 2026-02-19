"""
Analytics Module — GuardianAI
Computes display metrics from Excel report records.
All values derived from real crawl data. Nothing estimated.
"""

from collections import Counter


def generate_metrics(records):
    """
    Takes report rows (from Excel/dict) and returns summary metrics
    for the frontend. All None-safe.
    """
    if not records:
        return None

    statuses = []
    load_times = []
    health_scores = []
    perf_scores = []
    a11y_scores = []
    sec_scores = []
    a11y_issues = []
    broken_link_counts = []
    js_error_counts = []

    for r in records:
        # HTTP status
        try:
            statuses.append(int(r.get("Status", 0)))
        except (TypeError, ValueError):
            statuses.append(0)

        # Load time
        try:
            val = r.get("Load Time (s)", r.get("Load Time", 0))
            load_times.append(float(val))
        except (TypeError, ValueError):
            load_times.append(0.0)

        # Health score
        try:
            hs = r.get("Health Score")
            if hs not in (None, "", "None"):
                health_scores.append(float(hs))
        except (TypeError, ValueError):
            pass

        # Component scores
        for key, target_list in [
            ("Performance Score", perf_scores),
            ("Accessibility Score", a11y_scores),
            ("Security Score", sec_scores),
        ]:
            try:
                val = r.get(key)
                if val not in (None, "", "None"):
                    target_list.append(float(val))
            except (TypeError, ValueError):
                pass

        # Issue counts
        try:
            ai = r.get("Accessibility Issues", 0)
            a11y_issues.append(int(float(ai)) if ai not in (None, "", "None") else 0)
        except (TypeError, ValueError):
            a11y_issues.append(0)

        try:
            bl = r.get("Broken Links", 0)
            broken_link_counts.append(int(float(bl)) if bl not in (None, "", "None") else 0)
        except (TypeError, ValueError):
            broken_link_counts.append(0)

        try:
            je = r.get("JS Errors", 0)
            js_error_counts.append(int(float(je)) if je not in (None, "", "None") else 0)
        except (TypeError, ValueError):
            js_error_counts.append(0)

    counter = Counter(statuses)
    avg_load = round(sum(load_times) / len(load_times), 2) if load_times else None
    slow_pages = sum(1 for t in load_times if t > 3.0)

    def _avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else None

    return {
        "total_pages": len(records),
        "active_pages": counter.get(200, 0),
        "not_found_pages": counter.get(404, 0),
        "server_errors": sum(v for k, v in counter.items() if 500 <= k < 600),
        "avg_load_time": avg_load,
        "slow_pages": slow_pages,
        "total_a11y_issues": sum(a11y_issues),
        "total_broken_links": sum(broken_link_counts),
        "total_js_errors": sum(js_error_counts),
        "avg_health_score": _avg(health_scores),
        "avg_performance_score": _avg(perf_scores),
        "avg_accessibility_score": _avg(a11y_scores),
        "avg_security_score": _avg(sec_scores),
        "score_distribution": {
            "Excellent": sum(1 for s in health_scores if s >= 90),
            "Good": sum(1 for s in health_scores if 75 <= s < 90),
            "Needs Attention": sum(1 for s in health_scores if 50 <= s < 75),
            "Critical": sum(1 for s in health_scores if s < 50),
        }
    }