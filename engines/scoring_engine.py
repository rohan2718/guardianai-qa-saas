"""
engines/scoring_engine.py — GuardianAI Production Refactor
Key change: compute_functional_score now uses ONLY broken_navigation_links
for deductions. failed_assets and third_party_failures do NOT affect score.
"""

from typing import Optional

WEIGHTS = {
    "performance":   0.30,
    "accessibility": 0.25,
    "security":      0.20,
    "functional":    0.15,
    "ui_form":       0.10,
}

RISK_THRESHOLDS = {
    "Excellent":       90,
    "Good":            75,
    "Needs Attention": 50,
    "Critical":         0,
}


def compute_functional_score(page_data: dict) -> dict:
    """
    Computes functional health score.

    CRITICAL CHANGE: Only broken_navigation_links (internal <a href> anchor
    links returning 4xx/5xx) count toward deductions.
    failed_assets (images/fonts/scripts) and third_party_failures are
    informational only and do NOT reduce this score.

    Deductions:
      - HTTP status errors:        -50 (4xx) to -100 (5xx)
      - Broken nav links:          -7 per link, max -35
      - JS errors:                 -3 per error, max -20
      - Redirect chain > 2:        -3 per extra hop, max -10
    """
    status        = page_data.get("status", 200)
    redirect_chain = page_data.get("redirect_chain_length", 0) or 0
    js_errors      = page_data.get("js_errors") or []

    # REFACTORED: Use broken_navigation_links ONLY
    # Falls back to broken_links for backward compatibility during migration
    broken_nav_links = (
        page_data.get("broken_navigation_links") or
        page_data.get("broken_links") or
        []
    )

    score     = 100.0
    breakdown = {}

    # HTTP status
    if status == 404:
        score -= 80
        breakdown["http_status"] = {"value": status, "deduction": 80}
    elif status == 500:
        score -= 100
        breakdown["http_status"] = {"value": status, "deduction": 100}
    elif status and status >= 400:
        score -= 50
        breakdown["http_status"] = {"value": status, "deduction": 50}
    else:
        breakdown["http_status"] = {"value": status, "deduction": 0}

    # Broken NAVIGATION links only (not assets, not 3rd party)
    broken_count = len(broken_nav_links)
    if broken_count > 0:
        b_deduct = min(35.0, broken_count * 7.0)
        score -= b_deduct
        breakdown["broken_navigation_links"] = {"count": broken_count, "deduction": round(b_deduct, 1)}
    else:
        breakdown["broken_navigation_links"] = {"count": 0, "deduction": 0}

    # JS console errors
    js_count = len(js_errors)
    if js_count > 0:
        j_deduct = min(20.0, js_count * 3.0)
        score -= j_deduct
        breakdown["js_errors"] = {"count": js_count, "deduction": round(j_deduct, 1)}
    else:
        breakdown["js_errors"] = {"count": 0, "deduction": 0}

    # Redirect chains (instability signal)
    if redirect_chain > 2:
        r_deduct = min(10.0, (redirect_chain - 2) * 3.0)
        score -= r_deduct
        breakdown["redirect_chain"] = {"length": redirect_chain, "deduction": round(r_deduct, 1)}
    else:
        breakdown["redirect_chain"] = {"length": redirect_chain, "deduction": 0}

    # Informational only — not scored
    breakdown["failed_assets"]        = {"count": len(page_data.get("failed_assets") or []),        "scored": False}
    breakdown["third_party_failures"] = {"count": len(page_data.get("third_party_failures") or []), "scored": False}

    score = max(0.0, min(100.0, score))
    return {
        "score":     round(score, 1),
        "breakdown": breakdown,
    }


def compute_ui_form_score(page_data: dict) -> dict:
    """
    Scores UI element integrity and form health from crawl data.
    """
    score     = 100.0
    breakdown = {}

    forms = page_data.get("forms") or []
    if forms:
        broken_forms = [f for f in forms if isinstance(f, dict) and f.get("has_issues")]
        if broken_forms:
            f_deduct = min(40.0, len(broken_forms) * 10.0)
            score -= f_deduct
            breakdown["form_issues"] = {"count": len(broken_forms), "deduction": round(f_deduct, 1)}
        else:
            breakdown["form_issues"] = {"count": 0, "deduction": 0}
    else:
        breakdown["form_issues"] = {"count": 0, "deduction": 0}

    ui_elements = page_data.get("ui_elements") or []
    broken_ui   = [e for e in ui_elements if isinstance(e, dict) and e.get("has_issues")]
    if broken_ui:
        u_deduct = min(20.0, len(broken_ui) * 5.0)
        score -= u_deduct
        breakdown["ui_issues"] = {"count": len(broken_ui), "deduction": round(u_deduct, 1)}
    else:
        breakdown["ui_issues"] = {"count": 0, "deduction": 0}

    score = max(0.0, min(100.0, score))
    return {
        "score":     round(score, 1),
        "breakdown": breakdown,
    }


def compute_page_health_score(
    performance_score:   Optional[float] = None,
    accessibility_score: Optional[float] = None,
    security_score:      Optional[float] = None,
    functional_score:    Optional[float] = None,
    ui_form_score:       Optional[float] = None,
) -> dict:
    """
    Computes weighted composite health score from component scores.
    Handles None scores by redistributing weight to present components.
    """
    component_map = {
        "performance":   performance_score,
        "accessibility": accessibility_score,
        "security":      security_score,
        "functional":    functional_score,
        "ui_form":       ui_form_score,
    }

    present = {k: v for k, v in component_map.items() if v is not None}

    if not present:
        return {
            "health_score":  None,
            "risk_category": "Unknown",
            "components":    component_map,
        }

    total_weight   = sum(WEIGHTS[k] for k in present)
    weighted_total = sum(WEIGHTS[k] * v for k, v in present.items())
    health_score   = round(weighted_total / total_weight, 1)

    risk_category = "Critical"
    for label, threshold in sorted(RISK_THRESHOLDS.items(), key=lambda x: -x[1]):
        if health_score >= threshold:
            risk_category = label
            break

    return {
        "health_score":  health_score,
        "risk_category": risk_category,
        "components":    component_map,
    }


def compute_site_health_score(page_health_list: list) -> dict:
    """
    Aggregates per-page health breakdowns into a site-level summary.
    """
    if not page_health_list:
        return {"site_health_score": None, "risk_category": "Unknown", "page_count": 0}

    scores = [p.get("health_score") for p in page_health_list if p and p.get("health_score") is not None]

    if not scores:
        return {"site_health_score": None, "risk_category": "Unknown", "page_count": len(page_health_list)}

    avg_score = round(sum(scores) / len(scores), 1)
    min_score = round(min(scores), 1)

    risk_category = "Critical"
    for label, threshold in sorted(RISK_THRESHOLDS.items(), key=lambda x: -x[1]):
        if avg_score >= threshold:
            risk_category = label
            break

    return {
        "site_health_score": avg_score,
        "min_health_score":  min_score,
        "risk_category":     risk_category,
        "page_count":        len(page_health_list),
    }