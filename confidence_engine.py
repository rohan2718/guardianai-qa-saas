"""
Confidence Engine — GuardianAI
Computes confidence_score (0–100) per page based on:
  - checks_executed vs total possible checks
  - null metric ratio
  - execution completeness

No fabrication. If data missing → null metrics → lower confidence.
"""

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

# Total possible check categories
POSSIBLE_CHECKS = [
    "performance_score",
    "accessibility_score",
    "security_score",
    "functional_score",
    "ui_form_score",
    "fcp_ms",
    "lcp_ms",
    "ttfb_ms",
    "load_time",
    "accessibility_issues",
    "is_https",
]

# Weighted importance of each check (weights sum to 1.0)
CHECK_WEIGHTS = {
    "performance_score":    0.15,
    "accessibility_score":  0.15,
    "security_score":       0.15,
    "functional_score":     0.10,
    "ui_form_score":        0.05,
    "fcp_ms":               0.10,
    "lcp_ms":               0.10,
    "ttfb_ms":              0.08,
    "load_time":            0.07,
    "accessibility_issues": 0.03,
    "is_https":             0.02,
}


def compute_confidence_score(page_data: dict, active_filters: list = None) -> dict:
    """
    Computes confidence_score for a single page result.

    Args:
        page_data: The full page result dict from crawler
        active_filters: list of filter keys that were selected (if None, all assumed active)

    Returns:
        {
            "confidence_score": float|null,
            "checks_executed": int,
            "checks_null": int,
            "completeness_ratio": float
        }
    """
    if not page_data:
        return {
            "confidence_score": None,
            "checks_executed": 0,
            "checks_null": len(POSSIBLE_CHECKS),
            "completeness_ratio": 0.0
        }

    # Determine which checks were expected based on active_filters
    # If no filter passed, expect all
    expected_checks = POSSIBLE_CHECKS[:]
    if active_filters:
        # Map filter names to check keys
        filter_check_map = {
            "performance": ["performance_score", "fcp_ms", "lcp_ms", "ttfb_ms", "load_time"],
            "accessibility": ["accessibility_score", "accessibility_issues"],
            "security": ["security_score", "is_https"],
            "functional": ["functional_score"],
            "ui_form": ["ui_form_score"],
        }
        expected_checks = []
        for f in active_filters:
            expected_checks.extend(filter_check_map.get(f, []))
        expected_checks = list(set(expected_checks)) or POSSIBLE_CHECKS[:]

    executed = 0
    null_count = 0
    weighted_score = 0.0
    weight_total = 0.0

    for check in expected_checks:
        val = page_data.get(check)
        weight = CHECK_WEIGHTS.get(check, 0.05)
        weight_total += weight

        if val is not None:
            executed += 1
            weighted_score += weight
        else:
            null_count += 1

    if weight_total == 0:
        return {
            "confidence_score": None,
            "checks_executed": 0,
            "checks_null": len(expected_checks),
            "completeness_ratio": 0.0
        }

    completeness_ratio = weighted_score / weight_total
    confidence = round(completeness_ratio * 100, 1)

    return {
        "confidence_score": confidence,
        "checks_executed": executed,
        "checks_null": null_count,
        "completeness_ratio": round(completeness_ratio, 4)
    }


def compute_run_confidence(pages: list, active_filters: list = None) -> float:
    """
    Aggregates per-page confidence scores into a single run-level confidence score.
    Returns null if no pages available.
    """
    if not pages:
        return None

    scores = []
    for p in pages:
        result = compute_confidence_score(p, active_filters)
        cs = result.get("confidence_score")
        if cs is not None:
            scores.append(cs)

    if not scores:
        return None

    return round(sum(scores) / len(scores), 1)


# ── AI Learning Fields ────────────────────────────────────────────────────────

def compute_failure_pattern_id(page_data: dict) -> str | None:
    """
    Generates a deterministic hash from the dominant failure pattern.
    Based on: top issue category + risk_category + status_code.
    Returns None if no issues found.
    """
    try:
        parts = []

        # Top accessibility issue type
        a11y = page_data.get("accessibility_data") or {}
        issues = a11y.get("issues") or []
        if issues:
            top_issue = issues[0]
            parts.append(f"a11y:{top_issue.get('category','unknown')}")

        # Top security finding category
        sec = page_data.get("security_data") or {}
        findings = sec.get("findings") or []
        if findings:
            top_sec = findings[0]
            parts.append(f"sec:{top_sec.get('category','unknown')}")

        # Broken link presence
        if page_data.get("broken_links"):
            parts.append("func:broken_links")

        # JS errors
        if page_data.get("js_errors"):
            parts.append("func:js_errors")

        # Performance grade
        grade = page_data.get("performance_grade")
        if grade and grade != "Excellent":
            parts.append(f"perf:{grade.lower().replace(' ','_')}")

        if not parts:
            return None

        raw = "|".join(sorted(parts)) + f"|status:{page_data.get('status',0)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    except Exception as e:
        logger.warning(f"failure_pattern_id computation failed: {e}")
        return None


def compute_root_cause_tag(page_data: dict) -> str | None:
    """
    Generates a human-readable root cause tag summarizing top failure patterns.
    No AI required. Pure deterministic tagging.
    """
    try:
        tags = []

        a11y = page_data.get("accessibility_data") or {}
        checks = a11y.get("checks") or {}
        if checks.get("missing_alt", 0) > 0:
            tags.append("missing_alt")
        if checks.get("unlabeled_inputs", 0) > 0:
            tags.append("unlabeled_inputs")
        if not a11y.get("has_lang_attr"):
            tags.append("no_lang_attr")

        sec = page_data.get("security_data") or {}
        findings = sec.get("findings") or []
        sec_cats = list({f.get("category") for f in findings if f.get("severity") in ("critical", "high")})
        tags.extend(sec_cats[:2])

        if page_data.get("broken_links"):
            tags.append("broken_links")

        perf = page_data.get("performance_metrics") or {}
        lcp = perf.get("lcp_ms")
        if lcp and lcp > 4000:
            tags.append("slow_lcp")

        if not tags:
            return None

        return "+".join(tags[:5])

    except Exception as e:
        logger.warning(f"root_cause_tag computation failed: {e}")
        return None


def compute_self_healing_suggestion(page_data: dict) -> str | None:
    """
    Generates concrete self-healing locator suggestions based on top issues.
    All deterministic — no AI call required.
    Returns None if no actionable suggestions.
    """
    try:
        suggestions = []

        a11y = page_data.get("accessibility_data") or {}
        checks = a11y.get("checks") or {}

        if checks.get("missing_alt", 0) > 0:
            suggestions.append("Fix: page.locator('img:not([alt])') → add alt attributes via DOM patch")

        if checks.get("unlabeled_inputs", 0) > 0:
            suggestions.append("Fix: page.locator('input:not([aria-label]):not([id])') → inject aria-label")

        sec = page_data.get("security_data") or {}
        findings = sec.get("findings") or []
        for f in findings:
            if f.get("category") == "csp":
                suggestions.append("Fix: Inject Content-Security-Policy header in server response")
                break
            if f.get("category") == "csrf":
                suggestions.append("Fix: page.locator('form[method=POST]') → inject hidden CSRF token input")
                break

        broken = page_data.get("broken_links") or []
        if broken:
            suggestions.append(f"Fix: {len(broken)} broken link(s) → run link-checker and redirect to active URL")

        if not suggestions:
            return None

        return " | ".join(suggestions[:3])

    except Exception as e:
        logger.warning(f"self_healing_suggestion failed: {e}")
        return None


def enrich_page_with_ai_fields(page_data: dict, active_filters: list = None) -> dict:
    """
    Adds all AI learning + confidence fields to page_data in-place.
    Called per page after scoring.
    """
    confidence_result = compute_confidence_score(page_data, active_filters)
    page_data["confidence_score"] = confidence_result.get("confidence_score")
    page_data["checks_executed"] = confidence_result.get("checks_executed")
    page_data["checks_null"] = confidence_result.get("checks_null")

    page_data["failure_pattern_id"] = compute_failure_pattern_id(page_data)
    page_data["root_cause_tag"] = compute_root_cause_tag(page_data)
    page_data["self_healing_suggestion"] = compute_self_healing_suggestion(page_data)
    page_data["similar_issue_ref"] = None   # DB lookup done in tasks.py
    page_data["ai_confidence"] = None       # Set if Cohere available

    return page_data