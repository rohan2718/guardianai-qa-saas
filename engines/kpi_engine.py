"""
engines/kpi_engine.py — GuardianAI KPI Calculation Engine
==========================================================
Derives the five dashboard KPI scores from raw page data and test execution
results.  This is the SINGLE source of truth for every number shown on the
KPI cards.  It never raises — every function returns a safe default on error.

Scores
------
  performance   (0-100)  weighted from load_time, FCP, LCP, TTFB
  accessibility (0-100)  weighted from a11y issue counts and severity
  security      (0-100)  weighted from security findings
  functional    (0-100)  weighted from broken links, JS errors, HTTP failures,
                         and test-execution pass rate
  ui_form       (0-100)  weighted from form health, UI issues, test failures

All five roll up into a composite site_health_score with configurable weights.

Integration
-----------
  Called from tasks.py run_qa_pipeline() AFTER test execution completes.
  Results are written to TestRun columns and returned for immediate use.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Composite weights (must sum to 1.0) ───────────────────────────────────────
COMPOSITE_WEIGHTS = {
    "performance":   0.20,
    "accessibility": 0.25,
    "security":      0.25,
    "functional":    0.20,
    "ui_form":       0.10,
}

# ── Risk thresholds ───────────────────────────────────────────────────────────
RISK_THRESHOLDS = [
    (90, "Excellent"),
    (75, "Good"),
    (50, "Needs Attention"),
    (0,  "Critical"),
]

def _risk(score: Optional[float]) -> str:
    if score is None:
        return "Unknown"
    for threshold, label in RISK_THRESHOLDS:
        if score >= threshold:
            return label
    return "Critical"

def _clamp(v: float) -> float:
    return max(0.0, min(100.0, round(v, 1)))

def _avg(vals: list) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return round(sum(clean) / len(clean), 1) if clean else None


# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE KPI
# Inputs: load_time (ms), fcp_ms, lcp_ms, ttfb_ms per page
# ══════════════════════════════════════════════════════════════════════════════

def _score_load_time(ms: Optional[float]) -> float:
    """100 = <1 s, degrades linearly to 0 at 10 s."""
    if ms is None:
        return 50.0   # neutral — don't punish for missing data
    if ms <= 1000:
        return 100.0
    if ms >= 10000:
        return 0.0
    return round(100.0 - (ms - 1000) / 9000 * 100, 1)

def _score_fcp(ms: Optional[float]) -> float:
    """100 = <1.8 s, 0 = >4 s  (Core Web Vitals thresholds)."""
    if ms is None:
        return 50.0
    if ms <= 1800:
        return 100.0
    if ms >= 4000:
        return 0.0
    return round(100.0 - (ms - 1800) / 2200 * 100, 1)

def _score_lcp(ms: Optional[float]) -> float:
    """100 = <2.5 s, 0 = >4 s."""
    if ms is None:
        return 50.0
    if ms <= 2500:
        return 100.0
    if ms >= 4000:
        return 0.0
    return round(100.0 - (ms - 2500) / 1500 * 100, 1)

def _score_ttfb(ms: Optional[float]) -> float:
    """100 = <200 ms, 0 = >2 s."""
    if ms is None:
        return 50.0
    if ms <= 200:
        return 100.0
    if ms >= 2000:
        return 0.0
    return round(100.0 - (ms - 200) / 1800 * 100, 1)

def compute_performance_kpi(pages: list[dict]) -> dict:
    """
    Derives performance KPI from all scanned pages.
    Returns {"score": float, "risk": str, "breakdown": dict, "slow_pages": int}.
    """
    if not pages:
        return {"score": None, "risk": "Unknown", "breakdown": {}, "slow_pages": 0}

    load_scores, fcp_scores, lcp_scores, ttfb_scores = [], [], [], []
    slow_pages = 0

    for p in pages:
        lt = p.get("load_time")
        if lt is not None:
            if lt > 3000:
                slow_pages += 1
            load_scores.append(_score_load_time(lt))
        fcp_scores.append(_score_fcp(p.get("fcp_ms")))
        lcp_scores.append(_score_lcp(p.get("lcp_ms")))
        ttfb_scores.append(_score_ttfb(p.get("ttfb_ms")))

    # Weighted blend: LCP matters most for perceived perf
    weights  = {"load": 0.25, "fcp": 0.25, "lcp": 0.30, "ttfb": 0.20}
    avg_load = _avg(load_scores) or 50
    avg_fcp  = _avg(fcp_scores)  or 50
    avg_lcp  = _avg(lcp_scores)  or 50
    avg_ttfb = _avg(ttfb_scores) or 50

    score = _clamp(
        avg_load * weights["load"] +
        avg_fcp  * weights["fcp"]  +
        avg_lcp  * weights["lcp"]  +
        avg_ttfb * weights["ttfb"]
    )

    return {
        "score": score,
        "risk":  _risk(score),
        "breakdown": {
            "avg_load_time_score": avg_load,
            "avg_fcp_score":       avg_fcp,
            "avg_lcp_score":       avg_lcp,
            "avg_ttfb_score":      avg_ttfb,
        },
        "slow_pages": slow_pages,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ACCESSIBILITY KPI
# Inputs: accessibility_issues count and severity breakdown per page
# ══════════════════════════════════════════════════════════════════════════════

def compute_accessibility_kpi(pages: list[dict]) -> dict:
    """
    Derives accessibility KPI.  Uses the pre-computed accessibility_score from
    the crawler's engine where available; falls back to raw issue counts.
    """
    if not pages:
        return {"score": None, "risk": "Unknown", "breakdown": {}, "total_issues": 0}

    scores      = []
    total_issues = 0

    for p in pages:
        existing = p.get("accessibility_score")
        if existing is not None:
            scores.append(float(existing))
        else:
            # Fallback: derive from issue count
            issues = p.get("accessibility_issues") or 0
            # Each issue deducts points; cap deduction at 100
            derived = _clamp(100.0 - min(100, issues * 4))
            scores.append(derived)

        total_issues += int(p.get("accessibility_issues") or 0)

    score = _avg(scores)

    return {
        "score":        score,
        "risk":         _risk(score),
        "breakdown":    {"page_scores": len(scores), "pages_with_issues": sum(1 for p in pages if (p.get("accessibility_issues") or 0) > 0)},
        "total_issues": total_issues,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECURITY KPI
# Inputs: security_score per page, is_https, security findings
# ══════════════════════════════════════════════════════════════════════════════

def compute_security_kpi(pages: list[dict]) -> dict:
    """
    Derives security KPI.  Prioritises pre-computed security_score; applies
    hard penalty for HTTP pages and missing security headers.
    """
    if not pages:
        return {"score": None, "risk": "Unknown", "breakdown": {}, "http_pages": 0}

    scores     = []
    http_pages = 0

    for p in pages:
        base = p.get("security_score")
        if base is not None:
            s = float(base)
        else:
            s = 70.0   # default — partial data

        # Hard penalty: page served over HTTP
        if p.get("is_https") is False:
            s = min(s, 30.0)
            http_pages += 1

        scores.append(_clamp(s))

    score = _avg(scores)

    return {
        "score":      score,
        "risk":       _risk(score),
        "breakdown":  {"pages_scored": len(scores)},
        "http_pages": http_pages,
    }


# ══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL KPI
# Inputs: broken links, JS errors, HTTP failures, test execution pass rate
# ══════════════════════════════════════════════════════════════════════════════

def compute_functional_kpi(
    pages: list[dict],
    test_results: list[dict] | None = None,
) -> dict:
    """
    Derives functional KPI.

    Crawl findings (60% weight):
      - Broken navigation links  (-10 pts each, cap 50)
      - JS errors                (-3 pts each,  cap 30)
      - HTTP 4xx/5xx pages       (-15 pts each, cap 50)

    Test execution pass rate (40% weight):
      - pass_rate = passed / total test cases
      - If no tests ran → this component is excluded (weight redistributed)
    """
    if not pages:
        return {"score": None, "risk": "Unknown", "breakdown": {}, "broken_links": 0, "js_errors": 0}

    total_broken = 0
    total_js     = 0
    http_failures = 0

    for p in pages:
        total_broken  += len(p.get("broken_navigation_links") or [])
        total_js      += len(p.get("js_errors") or [])
        status = p.get("status") or 200
        if status >= 400:
            http_failures += 1

    # Crawl score (0-100)
    crawl_score = 100.0
    crawl_score -= min(50.0, total_broken  * 10.0)
    crawl_score -= min(30.0, total_js      * 3.0)
    crawl_score -= min(50.0, http_failures * 15.0)
    crawl_score = _clamp(crawl_score)

    # Test execution score
    test_score   = None
    tests_passed = 0
    tests_total  = 0

    if test_results:
        tests_total  = len(test_results)
        tests_passed = sum(1 for r in test_results if r.get("status") == "pass")
        if tests_total > 0:
            test_score = _clamp(tests_passed / tests_total * 100)

    # Blend
    if test_score is not None:
        score = _clamp(crawl_score * 0.60 + test_score * 0.40)
    else:
        score = crawl_score

    return {
        "score": score,
        "risk":  _risk(score),
        "breakdown": {
            "crawl_score":   crawl_score,
            "test_score":    test_score,
            "tests_passed":  tests_passed,
            "tests_total":   tests_total,
        },
        "broken_links": total_broken,
        "js_errors":    total_js,
    }


# ══════════════════════════════════════════════════════════════════════════════
# UI / FORM KPI
# Inputs: form health scores, UI element issues, test failures on form flows
# ══════════════════════════════════════════════════════════════════════════════

def compute_ui_form_kpi(
    pages: list[dict],
    test_results: list[dict] | None = None,
) -> dict:
    """
    Derives UI/Form KPI.

    Page-level (70% weight):
      - Uses pre-computed ui_form_score where available
      - Penalises pages with broken forms (has_issues flag)

    Form test pass rate (30% weight):
      - Considers only test cases tagged as form/login/checkout flows
    """
    if not pages:
        return {"score": None, "risk": "Unknown", "breakdown": {}, "broken_forms": 0}

    page_scores  = []
    broken_forms = 0

    for p in pages:
        uis = p.get("ui_form_score")
        if uis is not None:
            page_scores.append(float(uis))
        else:
            # Derive from form health
            forms   = p.get("forms") or []
            bad     = sum(1 for f in forms if isinstance(f, dict) and f.get("has_issues"))
            derived = _clamp(100.0 - bad * 15.0)
            page_scores.append(derived)
        broken_forms += sum(
            1 for f in (p.get("forms") or [])
            if isinstance(f, dict) and f.get("has_issues")
        )

    page_score = _avg(page_scores) or 100.0

    # Form-specific test pass rate
    form_test_score = None
    if test_results:
        form_tests = [
            r for r in test_results
            if any(tag in (r.get("tags") or []) for tag in ("form", "login", "checkout", "registration"))
        ]
        if form_tests:
            passed = sum(1 for r in form_tests if r.get("status") == "pass")
            form_test_score = _clamp(passed / len(form_tests) * 100)

    if form_test_score is not None:
        score = _clamp(page_score * 0.70 + form_test_score * 0.30)
    else:
        score = page_score

    return {
        "score": score,
        "risk":  _risk(score),
        "breakdown": {
            "page_score":       page_score,
            "form_test_score":  form_test_score,
        },
        "broken_forms": broken_forms,
    }


# ══════════════════════════════════════════════════════════════════════════════
# COMPOSITE SITE HEALTH SCORE
# ══════════════════════════════════════════════════════════════════════════════

def compute_composite_kpis(
    pages: list[dict],
    test_results: list[dict] | None = None,
) -> dict:
    """
    Master function: computes all five KPI scores and the composite health score.

    Returns a flat dict ready to be unpacked into TestRun columns:
    {
        "avg_performance_score":   float | None,
        "avg_accessibility_score": float | None,
        "avg_security_score":      float | None,
        "avg_functional_score":    float | None,
        "avg_ui_form_score":       float | None,
        "site_health_score":       float | None,
        "risk_category":           str,
        "kpi_breakdown":           dict,      # full detail for each KPI
        "slow_pages_count":        int,
        "total_broken_links":      int,
        "total_js_errors":         int,
        "total_accessibility_issues": int,
    }
    """
    try:
        perf_kpi  = compute_performance_kpi(pages)
        a11y_kpi  = compute_accessibility_kpi(pages)
        sec_kpi   = compute_security_kpi(pages)
        func_kpi  = compute_functional_kpi(pages, test_results)
        ui_kpi    = compute_ui_form_kpi(pages, test_results)

        components = {
            "performance":   perf_kpi["score"],
            "accessibility": a11y_kpi["score"],
            "security":      sec_kpi["score"],
            "functional":    func_kpi["score"],
            "ui_form":       ui_kpi["score"],
        }

        # Weighted composite — skip None components and redistribute weight
        present = {k: v for k, v in components.items() if v is not None}
        if present:
            total_w = sum(COMPOSITE_WEIGHTS[k] for k in present)
            composite = _clamp(
                sum(COMPOSITE_WEIGHTS[k] * v for k, v in present.items()) / total_w
            )
        else:
            composite = None

        risk = _risk(composite)

        logger.info(
            f"[kpi_engine] composite={composite} | "
            f"perf={perf_kpi['score']} a11y={a11y_kpi['score']} "
            f"sec={sec_kpi['score']} func={func_kpi['score']} ui={ui_kpi['score']}"
        )

        return {
            "avg_performance_score":      perf_kpi["score"],
            "avg_accessibility_score":    a11y_kpi["score"],
            "avg_security_score":         sec_kpi["score"],
            "avg_functional_score":       func_kpi["score"],
            "avg_ui_form_score":          ui_kpi["score"],
            "site_health_score":          composite,
            "risk_category":              risk,
            "kpi_breakdown": {
                "performance":   perf_kpi,
                "accessibility": a11y_kpi,
                "security":      sec_kpi,
                "functional":    func_kpi,
                "ui_form":       ui_kpi,
            },
            "slow_pages_count":            perf_kpi.get("slow_pages", 0),
            "total_broken_links":          func_kpi.get("broken_links", 0),
            "total_js_errors":             func_kpi.get("js_errors", 0),
            "total_accessibility_issues":  a11y_kpi.get("total_issues", 0),
        }

    except Exception as e:
        logger.error(f"[kpi_engine] compute_composite_kpis failed: {e}", exc_info=True)
        return {
            "avg_performance_score":      None,
            "avg_accessibility_score":    None,
            "avg_security_score":         None,
            "avg_functional_score":       None,
            "avg_ui_form_score":          None,
            "site_health_score":          None,
            "risk_category":              "Unknown",
            "kpi_breakdown":              {},
            "slow_pages_count":           0,
            "total_broken_links":         0,
            "total_js_errors":            0,
            "total_accessibility_issues": 0,
        }