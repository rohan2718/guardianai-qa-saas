"""
dashboard_intelligence.py — GuardianAI Production
Backend helpers for enhanced dashboard insights.
Call from app.py route that renders the run detail / index page.

Provides:
  - top_failure_patterns()      — Top 5 failure patterns aggregated
  - severity_distribution()     — For chart rendering
  - risk_heatmap()              — Per-page risk grid data
  - broken_link_source_map()    — Which pages have nav link failures
  - most_unstable_page()        — Page with highest variance from site avg
  - component_score_variance()  — Stddev per score dimension
  - confidence_explanation()    — Human-readable confidence tooltip data
  - crawl_coverage_summary()    — Coverage %
"""

import math
from collections import Counter, defaultdict
from typing import Optional


# ── Top Failure Patterns ──────────────────────────────────────────────────────

def top_failure_patterns(pages: list, top_n: int = 5) -> list:
    """
    Aggregates failure_pattern_id + root_cause_tag across pages.
    Returns top N patterns sorted by frequency.

    Each result: {pattern_id, root_cause_tag, count, example_url, severity_hint}
    """
    pattern_map = defaultdict(lambda: {"count": 0, "urls": [], "root_cause_tag": None})

    for p in pages:
        pid = p.get("failure_pattern_id")
        if not pid:
            continue
        pattern_map[pid]["count"] += 1
        pattern_map[pid]["root_cause_tag"] = p.get("root_cause_tag")
        if len(pattern_map[pid]["urls"]) < 3:
            pattern_map[pid]["urls"].append(p.get("url", ""))

    results = []
    for pid, data in sorted(pattern_map.items(), key=lambda x: -x[1]["count"]):
        tag    = data["root_cause_tag"] or ""
        # Map tag tokens to severity hints
        is_critical = any(t in tag for t in ["http_error", "sec_critical", "csp"])
        is_high     = any(t in tag for t in ["broken_nav", "slow_lcp", "no_https"])
        severity_hint = "critical" if is_critical else ("high" if is_high else "medium")

        results.append({
            "pattern_id":    pid,
            "root_cause_tag": tag,
            "count":         data["count"],
            "example_url":   data["urls"][0] if data["urls"] else "",
            "all_urls":      data["urls"],
            "severity_hint": severity_hint,
        })

    return results[:top_n]


# ── Severity Distribution ─────────────────────────────────────────────────────

def severity_distribution(pages: list) -> dict:
    """
    Returns counts per risk category for chart rendering.
    {Excellent: N, Good: N, Needs Attention: N, Critical: N, Unknown: N}
    """
    dist = Counter()
    for p in pages:
        cat = p.get("risk_category") or "Unknown"
        dist[cat] += 1

    return {
        "Excellent":       dist.get("Excellent", 0),
        "Good":            dist.get("Good", 0),
        "Needs Attention": dist.get("Needs Attention", 0),
        "Critical":        dist.get("Critical", 0),
        "Unknown":         dist.get("Unknown", 0),
    }


# ── Risk Heatmap ──────────────────────────────────────────────────────────────

def risk_heatmap(pages: list) -> list:
    """
    Returns per-page risk data suitable for a grid/heatmap.
    Sorted by health_score ascending (worst first).

    Each entry: {url, health_score, risk_category, broken_nav, js_errors, a11y_issues}
    """
    result = []
    for p in pages:
        health = p.get("health_score")
        result.append({
            "url":           p.get("url", ""),
            "title":         (p.get("title") or p.get("url", ""))[:60],
            "health_score":  health,
            "risk_category": p.get("risk_category") or "Unknown",
            "broken_nav":    len(p.get("broken_navigation_links") or p.get("broken_links") or []),
            "js_errors":     len(p.get("js_errors") or []),
            "a11y_issues":   p.get("accessibility_issues") or 0,
            "load_time":     p.get("load_time"),
            "status":        p.get("status"),
        })

    result.sort(key=lambda x: (x["health_score"] is None, x["health_score"] or 0))
    return result


# ── Broken Link Source Map ────────────────────────────────────────────────────

def broken_link_source_map(pages: list) -> list:
    """
    Returns pages that have broken navigation links, with link details.
    Only includes broken_navigation_links (not assets/3rd-party).

    Each entry: {source_url, broken_links: [{url, status}]}
    """
    result = []
    for p in pages:
        nav_broken = p.get("broken_navigation_links") or []
        if nav_broken:
            result.append({
                "source_url":    p.get("url", ""),
                "broken_links":  nav_broken,
                "broken_count":  len(nav_broken),
            })

    result.sort(key=lambda x: -x["broken_count"])
    return result


# ── Most Unstable Page ────────────────────────────────────────────────────────

def most_unstable_page(pages: list) -> Optional[dict]:
    """
    Returns the page whose health_score deviates most from the site average.
    Unstable = large component score divergence from site mean.
    """
    valid = [p for p in pages if p.get("health_score") is not None]
    if not valid:
        return None

    mean_health = sum(p["health_score"] for p in valid) / len(valid)
    worst       = max(valid, key=lambda p: abs(p["health_score"] - mean_health))

    return {
        "url":              worst.get("url"),
        "health_score":     worst.get("health_score"),
        "deviation":        round(abs(worst["health_score"] - mean_health), 1),
        "site_avg_health":  round(mean_health, 1),
        "risk_category":    worst.get("risk_category"),
        "root_cause_tag":   worst.get("root_cause_tag"),
    }


# ── Component Score Variance ──────────────────────────────────────────────────

def component_score_variance(pages: list) -> dict:
    """
    Returns mean + stddev per scoring dimension.
    High stddev = inconsistent quality across site.

    {performance: {mean, stddev}, accessibility: {...}, ...}
    """
    dims = ["performance_score", "accessibility_score", "security_score", "functional_score", "ui_form_score"]
    result = {}

    for dim in dims:
        values = [p.get(dim) for p in pages if p.get(dim) is not None]
        if not values:
            result[dim] = {"mean": None, "stddev": None, "count": 0}
            continue
        mean   = sum(values) / len(values)
        stddev = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
        result[dim] = {
            "mean":   round(mean, 1),
            "stddev": round(stddev, 1),
            "min":    round(min(values), 1),
            "max":    round(max(values), 1),
            "count":  len(values),
        }

    return result


# ── Confidence Explanation Tooltip ───────────────────────────────────────────

def confidence_explanation(pages: list, run_confidence: float, active_filters: list = None) -> dict:
    """
    Returns structured data for the confidence tooltip in the dashboard.
    Explains WHAT drove the confidence score up or down.
    """
    from confidence_engine import compute_confidence_explanation
    explanation = compute_confidence_explanation(pages, active_filters)
    factors = explanation.get("factors", {})

    coverage_pct = factors.get("crawl_coverage_pct", 100.0)
    broken_nav   = factors.get("broken_nav_links", 0)
    js_errors    = factors.get("js_errors_total", 0)
    redir_unstab = factors.get("unstable_redirects", 0)
    stddev       = factors.get("health_score_stddev", 0)
    discovered   = factors.get("pages_discovered", 0)
    scanned      = factors.get("pages_scanned", 0)

    # Build plain-English explanation lines
    lines = []
    if coverage_pct < 100:
        lines.append(f"Only {coverage_pct}% of discovered pages were scanned ({scanned}/{discovered})")
    if broken_nav > 0:
        lines.append(f"{broken_nav} broken navigation link(s) reduce link integrity factor")
    if js_errors > 0:
        lines.append(f"{js_errors} JS error(s) across site reduce cleanliness factor")
    if redir_unstab > 0:
        lines.append(f"{redir_unstab} page(s) with long redirect chains reduce stability factor")
    if stddev > 20:
        lines.append(f"High health score variance (σ={stddev}) indicates inconsistent quality")
    if not lines:
        lines.append("All factors strong — full crawl coverage, low errors, consistent scores")

    return {
        "score":        run_confidence,
        "factors":      factors,
        "explanation":  lines,
    }


# ── Crawl Coverage Summary ────────────────────────────────────────────────────

def crawl_coverage_summary(pages: list, max_pages: int = None) -> dict:
    """
    Returns coverage stats for dashboard header.
    """
    scanned = len(pages)
    discovered_urls = set()
    for p in pages:
        discovered_urls.add(p.get("url", ""))
        for cp in (p.get("connected_pages") or []):
            discovered_urls.add(cp)
    discovered_urls.discard("")
    total_discovered = len(discovered_urls)

    coverage_pct = round(min(100.0, scanned / max(1, total_discovered) * 100), 1)
    capped       = max_pages is not None and scanned >= max_pages

    return {
        "pages_scanned":    scanned,
        "pages_discovered": total_discovered,
        "coverage_pct":     coverage_pct,
        "capped_by_limit":  capped,
        "page_limit":       max_pages,
    }


# ── Master Intelligence Bundle ────────────────────────────────────────────────

def build_dashboard_intelligence(pages: list, run_confidence: float, active_filters: list = None, max_pages: int = None) -> dict:
    """
    Single call returns all intelligence data for dashboard rendering.
    Call this from app.py run detail route and pass to template as `intel`.
    """
    if not pages:
        return {
            "failure_patterns":    [],
            "severity_dist":       {},
            "risk_heatmap":        [],
            "broken_link_map":     [],
            "unstable_page":       None,
            "score_variance":      {},
            "confidence_tooltip":  {},
            "crawl_coverage":      {},
        }

    return {
        "failure_patterns":   top_failure_patterns(pages),
        "severity_dist":      severity_distribution(pages),
        "risk_heatmap":       risk_heatmap(pages),
        "broken_link_map":    broken_link_source_map(pages),
        "unstable_page":      most_unstable_page(pages),
        "score_variance":     component_score_variance(pages),
        "confidence_tooltip": confidence_explanation(pages, run_confidence, active_filters),
        "crawl_coverage":     crawl_coverage_summary(pages, max_pages),
    }